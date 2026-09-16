"""Standalone PaddleOCR-VL worker executed by an isolated Python runtime.

This module deliberately imports no ``manual_ingestion`` code and imports
Paddle only after the command starts.  It can therefore be invoked from a
dedicated environment whose dependencies differ from the main V2 package.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import platform
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

WORKER_SCHEMA_VERSION = "1.0"


class PaddleWorkerError(RuntimeError):
    pass


def probe_runtime(
    *,
    importer: Callable[[str], object] = importlib.import_module,
    version_reader: Callable[[str], str] = importlib.metadata.version,
) -> dict[str, str]:
    try:
        module = importer("paddleocr")
    except Exception as exc:
        raise PaddleWorkerError(
            "PaddleOCR-VL is unavailable in this Python runtime. Install the "
            "PaddleOCR document-parser dependencies in the isolated environment."
        ) from exc
    if not callable(getattr(module, "PaddleOCRVL", None)):
        raise PaddleWorkerError("paddleocr does not expose PaddleOCRVL")

    def version(name: str) -> str:
        try:
            return version_reader(name)
        except Exception:
            return "unknown"

    return {
        "python": platform.python_version(),
        "paddleocr": version("paddleocr"),
        "paddlepaddle": version("paddlepaddle"),
        "paddlex": version("paddlex"),
    }


def process_request(
    request: dict[str, Any],
    *,
    output_root: Path,
    paddle_factory: Callable[..., object] | None = None,
) -> dict[str, Any]:
    _validate_request(request)
    runtime = probe_runtime() if paddle_factory is None else {
        "python": platform.python_version(),
        "paddleocr": "injected",
        "paddlepaddle": "injected",
        "paddlex": "injected",
    }
    if paddle_factory is None:
        module = importlib.import_module("paddleocr")
        paddle_factory = getattr(module, "PaddleOCRVL")

    config = dict(request.get("config", {}))
    init_kwargs = _init_kwargs(config)
    predict_kwargs = _predict_kwargs(config)
    try:
        parser = paddle_factory(**init_kwargs)
    except Exception as exc:
        raise PaddleWorkerError(f"Cannot initialize PaddleOCRVL: {exc}") from exc

    output_root.mkdir(parents=True, exist_ok=True)
    native_results: list[object] = []
    pages: list[dict[str, Any]] = []
    for page_spec in request["pages"]:
        page_number = int(page_spec["page"])
        image_path = Path(page_spec["image_path"])
        if not image_path.is_file():
            raise PaddleWorkerError(
                f"Rendered image for page {page_number} does not exist: {image_path}"
            )
        page_dir = output_root / f"page_{page_number:04d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        native, raw = _predict_page(
            parser,
            image_path=image_path,
            page_dir=page_dir,
            predict_kwargs=predict_kwargs,
        )
        native_results.append(native)
        pages.append(
            {
                "page": page_number,
                "image_path": str(image_path),
                "raw": raw,
                "markdown": _read_first_markdown(page_dir) or _extract_markdown(raw),
            }
        )

    warnings: list[str] = []
    if config.get("restructure_pages", True) and native_results:
        restructured, restructure_warning = _restructure(parser, native_results, config)
        if restructure_warning:
            warnings.append(restructure_warning)
        if restructured is not None and len(restructured) == len(pages):
            for page, raw in zip(pages, restructured, strict=True):
                page["raw"] = raw
        elif restructured is not None:
            warnings.append(
                "Paddle restructure_pages returned a different page count; original page results were kept."
            )

    return {
        "schema_version": WORKER_SCHEMA_VERSION,
        "status": "completed",
        "runtime": runtime,
        "config": config,
        "warnings": warnings,
        "pages": pages,
    }


def _predict_page(
    parser: object,
    *,
    image_path: Path,
    page_dir: Path,
    predict_kwargs: dict[str, Any],
) -> tuple[object, object]:
    predict = getattr(parser, "predict", None)
    if not callable(predict):
        raise PaddleWorkerError("PaddleOCRVL does not expose predict()")
    try:
        result = predict(input=str(image_path), **predict_kwargs)
    except TypeError:
        result = predict(str(image_path), **predict_kwargs)
    except Exception as exc:
        raise PaddleWorkerError(f"Paddle prediction failed for {image_path.name}: {exc}") from exc

    items = (
        list(result)
        if not isinstance(result, (dict, str, bytes)) and hasattr(result, "__iter__")
        else [result]
    )
    if not items or items == [None]:
        raise PaddleWorkerError(f"Paddle returned no result for {image_path.name}")
    for index, item in enumerate(items, start=1):
        _save_optional_result_files(item, page_dir, index)
    native = items[0] if len(items) == 1 else items
    raw_items = [_jsonable_paddle_result(item) for item in items]
    raw = raw_items[0] if len(raw_items) == 1 else raw_items
    return native, raw


def _restructure(
    parser: object,
    native_results: list[object],
    config: dict[str, Any],
) -> tuple[list[object] | None, str | None]:
    restructure = getattr(parser, "restructure_pages", None)
    if not callable(restructure):
        return None, "Paddle runtime does not expose restructure_pages; original page results were kept."
    try:
        value = restructure(
            native_results,
            merge_tables=bool(config.get("restructure_merge_tables", True)),
            relevel_titles=bool(config.get("restructure_relevel_titles", True)),
            concatenate_pages=False,
        )
    except Exception as exc:
        return None, f"Paddle restructure_pages failed; original page results were kept: {exc}"
    items = (
        list(value)
        if not isinstance(value, (dict, str, bytes)) and hasattr(value, "__iter__")
        else [value]
    )
    return [_jsonable_paddle_result(item) for item in items], None


def _init_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "pipeline_version",
        "vl_rec_backend",
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_layout_detection",
        "use_chart_recognition",
        "use_seal_recognition",
        "use_ocr_for_image_block",
        "format_block_content",
        "merge_layout_blocks",
        "markdown_ignore_labels",
        "layout_threshold",
        "layout_nms",
        "layout_unclip_ratio",
        "layout_merge_bboxes_mode",
        "use_queues",
    }
    return {key: config[key] for key in keys if key in config and config[key] is not None}


def _predict_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_layout_detection",
        "use_chart_recognition",
        "use_seal_recognition",
        "use_ocr_for_image_block",
        "format_block_content",
        "merge_layout_blocks",
        "markdown_ignore_labels",
        "layout_threshold",
        "layout_nms",
        "layout_unclip_ratio",
        "layout_merge_bboxes_mode",
        "layout_shape_mode",
        "temperature",
        "top_p",
        "repetition_penalty",
        "min_pixels",
        "max_pixels",
        "max_new_tokens",
        "use_queues",
    }
    return {key: config[key] for key in keys if key in config and config[key] is not None}


def _save_optional_result_files(result: object, output_dir: Path, index: int) -> None:
    for method_name in ("save_to_json", "save_to_markdown"):
        method = getattr(result, method_name, None)
        if not callable(method):
            continue
        try:
            method(save_path=output_dir)
        except TypeError:
            method(output_dir)
        except Exception:
            pass
    raw_path = output_dir / f"result_{index:03d}.json"
    if not raw_path.exists():
        _write_json(raw_path, _jsonable_paddle_result(result))


def _jsonable_paddle_result(value: object) -> object:
    raw = _jsonable_result(value)
    if not isinstance(raw, dict):
        return raw
    result = raw.get("res")
    if not isinstance(result, dict) or not isinstance(result.get("parsing_res_list"), list):
        return raw
    try:
        blocks = value["parsing_res_list"]  # type: ignore[index]
    except Exception:
        return raw
    for raw_block, block in zip(result["parsing_res_list"], blocks, strict=False):
        if not isinstance(raw_block, dict):
            continue
        for attribute in ("title_level", "page_index"):
            candidate = getattr(block, attribute, None)
            if isinstance(candidate, int):
                raw_block[attribute] = candidate
    return raw


def _jsonable_result(value: object) -> object:
    for attribute in ("json_result", "json", "result", "data"):
        candidate = getattr(value, attribute, None)
        if candidate is not None and not callable(candidate):
            return _jsonable_result(candidate)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_result(item) for item in value]
    return str(value)


def _read_first_markdown(directory: Path) -> str:
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.casefold() in {".md", ".markdown"}:
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    return ""


def _extract_markdown(value: object) -> str:
    values: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in {"markdown", "md"} and isinstance(child, str):
                    values.append(child)
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return "\n\n".join(item.strip() for item in values if item.strip())


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise PaddleWorkerError("Unsupported Paddle worker request schema")
    pages = request.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PaddleWorkerError("Paddle worker request requires a non-empty pages list")
    seen: set[int] = set()
    for item in pages:
        if not isinstance(item, dict):
            raise PaddleWorkerError("Each Paddle page request must be an object")
        try:
            page = int(item["page"])
            image_path = str(item["image_path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaddleWorkerError("Invalid Paddle page request") from exc
        if page < 1 or page in seen or not image_path:
            raise PaddleWorkerError("Paddle page numbers must be unique positive integers")
        seen.add(page)
    config = request.get("config", {})
    if not isinstance(config, dict):
        raise PaddleWorkerError("Paddle worker config must be an object")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paddle-worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("probe")
    run = subparsers.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--response", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "probe":
        try:
            print(json.dumps({"status": "ready", "runtime": probe_runtime()}))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}))
            return 1

    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise PaddleWorkerError("Paddle worker request must contain a JSON object")
        response = process_request(request, output_root=args.output_root)
        _write_json(args.response, response)
        return 0
    except Exception as exc:
        failure = {
            "schema_version": WORKER_SCHEMA_VERSION,
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        try:
            _write_json(args.response, failure)
        except Exception:
            traceback.print_exc(file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
