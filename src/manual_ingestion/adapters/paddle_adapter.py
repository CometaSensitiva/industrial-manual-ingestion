"""Main-runtime adapter for an isolated PaddleOCR-VL worker process."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pymupdf
from PIL import Image

from manual_ingestion.models import (
    BoundingBox,
    ElementType,
    ManualElement,
    PageSize,
    SourceReference,
    TraceMetadata,
)
from manual_ingestion.profiles.base import ContentParseResult
from manual_ingestion.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStage,
    ProgressState,
    emit_progress,
)

from .paddle_worker import WORKER_SCHEMA_VERSION


PADDLE_RUNTIME_KEYS = ("python", "paddleocr", "paddlepaddle", "paddlex")
ACCEPTED_PADDLE_PACKAGE_VERSIONS = {
    "paddleocr": "3.7.0",
    "paddlepaddle": "3.2.1",
    "paddlex": "3.7.2",
}


class PaddleAdapterError(RuntimeError):
    """Raised when the external OCR runtime or its output is not trustworthy."""


@dataclass(frozen=True)
class PaddleOCRConfig:
    """Chosen, persisted PaddleOCR-VL setup for scanned manuals."""

    pipeline_version: str = "v1.6"
    vl_rec_backend: str = "native"
    use_doc_orientation_classify: bool = True
    use_doc_unwarping: bool = False
    use_layout_detection: bool = True
    use_chart_recognition: bool = False
    use_seal_recognition: bool = False
    use_ocr_for_image_block: bool = True
    format_block_content: bool = False
    merge_layout_blocks: bool = True
    markdown_ignore_labels: tuple[str, ...] = (
        "number",
        "footnote",
        "header",
        "header_image",
        "footer",
        "footer_image",
        "aside_text",
    )
    layout_shape_mode: str = "auto"
    restructure_pages: bool = True
    restructure_merge_tables: bool = True
    restructure_relevel_titles: bool = True
    temperature: float | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    max_new_tokens: int | None = None

    def to_worker_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["markdown_ignore_labels"] = list(self.markdown_ignore_labels)
        return value


@dataclass(frozen=True)
class PaddleRuntimeConfig:
    python_executable: str = field(
        default_factory=lambda: os.environ.get(
            "MANUAL_INGESTION_PADDLE_PYTHON",
            sys.executable,
        )
    )
    cache_dir: Path | None = None
    render_dpi: int = 200
    max_render_edge: int = 2600
    timeout_seconds_per_page: int = 600
    max_total_timeout_seconds: int = 7200
    pages_per_worker: int = 6

    def __post_init__(self) -> None:
        if not self.python_executable.strip():
            raise ValueError("Paddle python_executable cannot be empty")
        if self.render_dpi < 72:
            raise ValueError("Paddle render_dpi must be at least 72")
        if self.max_render_edge < 1000:
            raise ValueError("Paddle max_render_edge must be at least 1000 pixels")
        if self.timeout_seconds_per_page <= 0:
            raise ValueError("Paddle timeout_seconds_per_page must be positive")
        if self.max_total_timeout_seconds <= 0:
            raise ValueError("Paddle max_total_timeout_seconds must be positive")
        if self.pages_per_worker <= 0:
            raise ValueError("Paddle pages_per_worker must be positive")


class PaddleWorkerRunner(Protocol):
    def probe(self) -> dict[str, str]: ...

    def run(
        self,
        request: dict[str, Any],
        *,
        diagnostics_dir: Path,
        page_count: int,
    ) -> dict[str, Any]: ...


class SubprocessPaddleWorker:
    """Execute ``paddle_worker.py`` with a separately configured interpreter."""

    def __init__(self, config: PaddleRuntimeConfig) -> None:
        self.config = config
        self.worker_path = Path(__file__).with_name("paddle_worker.py")

    def probe(self) -> dict[str, str]:
        command = [self._executable(), str(self.worker_path), "probe"]
        completed = self._execute(command, timeout=120)
        payload = _last_json_object(completed.stdout)
        if completed.returncode != 0 or payload.get("status") != "ready":
            detail = payload.get("error") or completed.stderr.strip() or "unknown probe error"
            raise PaddleAdapterError(
                f"Paddle runtime probe failed for {self.config.python_executable}: {detail}"
            )
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict) or not all(
            isinstance(runtime.get(key), str)
            for key in PADDLE_RUNTIME_KEYS
        ):
            raise PaddleAdapterError("Paddle runtime probe returned malformed version metadata")
        return {key: str(runtime[key]) for key in PADDLE_RUNTIME_KEYS}

    def run(
        self,
        request: dict[str, Any],
        *,
        diagnostics_dir: Path,
        page_count: int,
    ) -> dict[str, Any]:
        diagnostics_dir.mkdir(parents=True, exist_ok=False)
        request_path = diagnostics_dir / "worker_request.json"
        response_path = diagnostics_dir / "worker_response.json"
        raw_dir = diagnostics_dir / "raw"
        _write_json(request_path, request)
        command = [
            self._executable(),
            str(self.worker_path),
            "run",
            "--request",
            str(request_path),
            "--response",
            str(response_path),
            "--output-root",
            str(raw_dir),
        ]
        completed = self._execute(
            command,
            timeout=self._timeout_for_pages(page_count),
        )
        response = _read_json_object(response_path)
        if completed.returncode != 0 or response.get("status") != "completed":
            detail = response.get("error") or completed.stderr.strip() or "unknown worker error"
            raise PaddleAdapterError(f"Paddle worker failed: {detail}")
        return response

    def _timeout_for_pages(self, page_count: int) -> int:
        """Bound the aggregate worker lifetime even when one prediction hangs."""

        return min(
            self.config.timeout_seconds_per_page * max(1, page_count),
            self.config.max_total_timeout_seconds,
        )

    def _executable(self) -> str:
        configured = self.config.python_executable
        if Path(configured).is_absolute() or "/" in configured:
            path = Path(configured).expanduser()
            if not path.is_file():
                raise PaddleAdapterError(f"Paddle Python executable not found: {path}")
            return str(path)
        resolved = shutil.which(configured)
        if not resolved:
            raise PaddleAdapterError(f"Paddle Python executable not found on PATH: {configured}")
        return resolved

    def _execute(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if self.config.cache_dir is not None:
            cache = str(self.config.cache_dir.expanduser().resolve())
            environment["PADDLE_PDX_CACHE_HOME"] = cache
            environment["PADDLEX_HOME"] = cache
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise PaddleAdapterError(
                f"Paddle worker timed out after {timeout} seconds"
            ) from exc
        except OSError as exc:
            raise PaddleAdapterError(f"Cannot start Paddle worker: {exc}") from exc


@dataclass(frozen=True)
class _RenderedPage:
    page: int
    path: Path
    width: int
    height: int


class PaddleOCRContentAdapter:
    """Render scanned pages, call Paddle externally, and normalize canonical elements."""

    name = "paddleocr_vl"

    def __init__(
        self,
        *,
        runtime: PaddleRuntimeConfig | None = None,
        ocr: PaddleOCRConfig | None = None,
        runner: PaddleWorkerRunner | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.runtime = runtime or PaddleRuntimeConfig()
        self.ocr = ocr or PaddleOCRConfig()
        self.runner = runner or SubprocessPaddleWorker(self.runtime)
        self.progress = progress
        self.last_runtime: dict[str, str] | None = None

    def preflight(self) -> dict[str, str]:
        """Verify the isolated Paddle runtime without rendering any PDF page."""

        if self.last_runtime is None:
            runtime = self.runner.probe()
            if not isinstance(runtime, dict) or not all(
                isinstance(runtime.get(key), str) and runtime[key].strip()
                for key in PADDLE_RUNTIME_KEYS
            ):
                raise PaddleAdapterError(
                    "Paddle runtime probe returned malformed version metadata"
                )
            self.last_runtime = {
                key: runtime[key].strip()
                for key in PADDLE_RUNTIME_KEYS
            }
            mismatches = [
                f"{package}={self.last_runtime[package]!r} "
                f"(accepted {accepted!r})"
                for package, accepted in ACCEPTED_PADDLE_PACKAGE_VERSIONS.items()
                if self.last_runtime[package] != accepted
            ]
            if not self.last_runtime["python"].startswith("3.12"):
                mismatches.insert(
                    0,
                    f"python={self.last_runtime['python']!r} (accepted '3.12.x')",
                )
            if mismatches:
                self.last_runtime = None
                raise PaddleAdapterError(
                    "Paddle runtime does not match the accepted scanned_ocr setup: "
                    + "; ".join(mismatches)
                )
        return dict(self.last_runtime)

    def public_config(self) -> dict[str, Any]:
        """Expose the exact chosen OCR setup; machine paths stay in runtime setup."""

        return {"ocr": self.ocr.to_worker_dict()}

    def parse(
        self,
        pdf_path: Path,
        pages: list[int],
        assets_dir: Path,
    ) -> ContentParseResult:
        source = Path(pdf_path)
        selected = _validate_pages(source, pages)
        assets_root = Path(assets_dir)
        # Fail before creating heavyweight page rasters if the isolated runtime
        # is unavailable or misconfigured.
        self.preflight()
        rendered = _render_pages(
            source,
            selected,
            assets_root / "pages",
            dpi=self.runtime.render_dpi,
            max_edge=self.runtime.max_render_edge,
        )
        elements: list[ManualElement] = []
        warnings: list[str] = []
        batches = list(_batched(rendered, self.runtime.pages_per_worker))
        diagnostics_root = assets_root.parent / "diagnostics" / "paddle"
        completed_pages = 0
        for batch_index, batch in enumerate(batches, start=1):
            request = {
                "schema_version": WORKER_SCHEMA_VERSION,
                "config": self.ocr.to_worker_dict(),
                "pages": [
                    {
                        "page": page.page,
                        "image_path": str(page.path.resolve()),
                        "width": page.width,
                        "height": page.height,
                    }
                    for page in batch
                ],
            }
            response = self.runner.run(
                request,
                diagnostics_dir=diagnostics_root / f"batch_{batch_index:04d}",
                page_count=len(batch),
            )
            _verify_worker_runtime(response, expected=self.last_runtime)
            batch_elements, batch_warnings = _normalize_response(
                response,
                rendered=batch,
                assets_root=assets_root,
            )
            elements.extend(batch_elements)
            warnings.extend(
                f"Paddle batch {batch_index}/{len(batches)}: {warning}"
                for warning in batch_warnings
            )
            completed_pages += len(batch)
            emit_progress(
                self.progress,
                ProgressEvent(
                    stage=ProgressStage.EXTRACTION,
                    state=ProgressState.UPDATED,
                    current=completed_pages,
                    total=len(selected),
                    unit="pages",
                    detail=f"Paddle batch {batch_index}/{len(batches)}",
                ),
            )
        pages_with_elements = {element.page for element in elements}
        missing = [page for page in selected if page not in pages_with_elements]
        if missing:
            raise PaddleAdapterError(
                f"Paddle returned no traceable elements for requested page(s): {missing}"
            )
        return ContentParseResult(
            elements=elements,
            pages_processed=selected,
            warnings=warnings,
        )


def _verify_worker_runtime(
    response: dict[str, Any],
    *,
    expected: dict[str, str] | None,
) -> None:
    runtime = response.get("runtime")
    if not isinstance(runtime, dict) or not all(
        isinstance(runtime.get(key), str) and runtime[key].strip()
        for key in PADDLE_RUNTIME_KEYS
    ):
        raise PaddleAdapterError(
            "Paddle worker response returned malformed version metadata"
        )
    actual = {key: runtime[key].strip() for key in PADDLE_RUNTIME_KEYS}
    if expected is None or actual != expected:
        raise PaddleAdapterError(
            "Paddle worker runtime differs from the accepted preflight runtime"
        )


def _batched(values: list[_RenderedPage], size: int) -> list[list[_RenderedPage]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _validate_pages(pdf_path: Path, pages: list[int]) -> list[int]:
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if not pages:
        raise ValueError("pages cannot be empty")
    if len(pages) != len(set(pages)):
        raise ValueError("pages cannot contain duplicates")
    selected = sorted(pages)
    if selected[0] < 1:
        raise ValueError("pages must use one-based positive numbers")
    try:
        with pymupdf.open(pdf_path) as document:
            if not document.is_pdf or document.needs_pass or document.page_count < 1:
                raise PaddleAdapterError("source must be a readable, non-empty PDF")
            pages_total = document.page_count
    except PaddleAdapterError:
        raise
    except Exception as exc:
        raise PaddleAdapterError(f"Cannot inspect scanned PDF {pdf_path}: {exc}") from exc
    if selected[-1] > pages_total:
        raise ValueError(f"page {selected[-1]} exceeds the PDF page count ({pages_total})")
    return selected


def _render_pages(
    pdf_path: Path,
    pages: list[int],
    output_dir: Path,
    *,
    dpi: int,
    max_edge: int,
) -> list[_RenderedPage]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[_RenderedPage] = []
    try:
        with pymupdf.open(pdf_path) as document:
            for page_number in pages:
                target = output_dir / f"page_{page_number:04d}.png"
                if target.exists():
                    raise PaddleAdapterError(f"rendered page already exists: {target}")
                page = document.load_page(page_number - 1)
                target_scale = dpi / 72
                edge_scale = max_edge / max(float(page.rect.width), float(page.rect.height))
                scale = min(target_scale, edge_scale)
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    alpha=False,
                )
                pixmap.save(target)
                rendered.append(
                    _RenderedPage(
                        page=page_number,
                        path=target,
                        width=pixmap.width,
                        height=pixmap.height,
                    )
                )
    except PaddleAdapterError:
        raise
    except Exception as exc:
        raise PaddleAdapterError(f"Cannot render scanned PDF pages: {exc}") from exc
    return rendered


def _normalize_response(
    response: dict[str, Any],
    *,
    rendered: list[_RenderedPage],
    assets_root: Path,
) -> tuple[list[ManualElement], list[str]]:
    if response.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise PaddleAdapterError("Paddle response uses an unsupported schema version")
    raw_pages = response.get("pages")
    if not isinstance(raw_pages, list):
        raise PaddleAdapterError("Paddle response has no pages array")
    expected = {page.page: page for page in rendered}
    received: dict[int, dict[str, Any]] = {}
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            raise PaddleAdapterError("Paddle page result must be an object")
        try:
            page_number = int(raw_page["page"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaddleAdapterError("Paddle page result has an invalid page number") from exc
        if page_number in received or page_number not in expected:
            raise PaddleAdapterError(
                f"Paddle returned a duplicate or unexpected page: {page_number}"
            )
        received[page_number] = raw_page
    if set(received) != set(expected):
        missing = sorted(set(expected) - set(received))
        raise PaddleAdapterError(f"Paddle response is missing requested pages: {missing}")

    elements: list[ManualElement] = []
    raw_warnings = response.get("warnings", [])
    if not isinstance(raw_warnings, list) or not all(
        isinstance(warning, str) for warning in raw_warnings
    ):
        raise PaddleAdapterError("Paddle response warnings must be a string array")
    warnings: list[str] = list(raw_warnings)
    counters: dict[tuple[int, ElementType], int] = {}
    for page_number in sorted(received):
        page = expected[page_number]
        raw = received[page_number].get("raw")
        angle = _document_orientation_angle(raw)
        blocks = _parsing_blocks(raw)
        if not blocks:
            raise PaddleAdapterError(
                f"Paddle page {page_number} contains no parsing_res_list blocks"
            )
        page_elements = _normalize_page_blocks(
            blocks,
            page=page,
            assets_root=assets_root,
            counters=counters,
            reading_offset=len(elements),
            warnings=warnings,
            orientation_angle=angle or 0,
        )
        if not page_elements:
            raise PaddleAdapterError(
                f"Paddle page {page_number} has blocks but no usable canonical elements"
            )
        elements.extend(page_elements)
    return elements, warnings


def _parsing_blocks(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        return []
    result: object = raw.get("res", raw)
    if not isinstance(result, dict):
        return []
    blocks = result.get("parsing_res_list")
    if not isinstance(blocks, list):
        return []
    return [block for block in blocks if isinstance(block, dict)]


def _normalize_page_blocks(
    blocks: list[dict[str, Any]],
    *,
    page: _RenderedPage,
    assets_root: Path,
    counters: dict[tuple[int, ElementType], int],
    reading_offset: int,
    warnings: list[str],
    orientation_angle: int,
) -> list[ManualElement]:
    indexed = list(enumerate(blocks))
    indexed.sort(key=lambda item: _block_sort_key(item[1], item[0]))
    elements: list[ManualElement] = []
    page_size = _rendered_page_size(page)
    for raw_index, block in indexed:
        role = str(block.get("block_label", block.get("label", "text"))).strip().casefold()
        if role in _FURNITURE_LABELS:
            continue
        element_type = _element_type(role, block)
        raw_text = _block_text(block)
        text = (
            raw_text.strip()
            if element_type is ElementType.TABLE
            else _normalize_text_lines(raw_text)
        )
        if (
            element_type is ElementType.TEXT
            and "\n" not in text
            and _looks_like_embedded_title(text)
        ):
            element_type = ElementType.TITLE
        bbox = _block_bbox(
            block,
            page_size,
            orientation_angle=orientation_angle,
        )
        if element_type in {ElementType.TITLE, ElementType.TEXT} and not text:
            warnings.append(f"Paddle page {page.page} block {raw_index} skipped: empty {role}")
            continue
        if element_type in {ElementType.IMAGE, ElementType.TABLE} and bbox is None:
            raise PaddleAdapterError(
                f"Paddle page {page.page} visual block {raw_index} has no valid bbox"
            )

        if element_type is ElementType.TEXT:
            segments = _split_embedded_titles(text)
            if any(segment_type is ElementType.TITLE for segment_type, _ in segments):
                for segment_type, segment_text in segments:
                    key = (page.page, segment_type)
                    counters[key] = counters.get(key, 0) + 1
                    element_id = (
                        f"paddle-{segment_type.value}-{page.page:04d}-{counters[key]:04d}"
                    )
                    elements.append(
                        ManualElement(
                            id=element_id,
                            type=segment_type,
                            page=page.page,
                            source=SourceReference(
                                page=page.page,
                                page_size=page_size,
                                bbox=bbox,
                            ),
                            text=segment_text,
                            trace=TraceMetadata(
                                reading_order=reading_offset + len(elements),
                                role=(
                                    f"{role}:embedded_title"
                                    if segment_type is ElementType.TITLE
                                    else role or None
                                ),
                            ),
                        )
                    )
                continue

        key = (page.page, element_type)
        counters[key] = counters.get(key, 0) + 1
        element_id = (
            f"paddle-{element_type.value}-{page.page:04d}-{counters[key]:04d}"
        )
        common: dict[str, Any] = {
            "id": element_id,
            "type": element_type,
            "page": page.page,
            "source": SourceReference(page=page.page, page_size=page_size, bbox=bbox),
            "trace": TraceMetadata(
                reading_order=reading_offset + len(elements),
                role=role or None,
                heading_level=_positive_heading_level(block),
            ),
        }
        if element_type is ElementType.IMAGE:
            image_path = _save_crop(
                page.path,
                bbox,
                assets_root / "images" / f"{element_id}.png",
                assets_root,
            )
            elements.append(
                ManualElement(
                    **common,
                    image_path=image_path,
                    caption_original=text or None,
                )
            )
        elif element_type is ElementType.TABLE:
            table_path = _save_crop(
                page.path,
                bbox,
                assets_root / "tables" / f"{element_id}.png",
                assets_root,
            )
            elements.append(
                ManualElement(
                    **common,
                    table_image_path=table_path,
                    table_markdown=text or None,
                )
            )
        else:
            elements.append(ManualElement(**common, text=text))
    return elements


_FURNITURE_LABELS = {
    "number",
    "footnote",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "aside_text",
    "page_header",
    "page_footer",
}
_TITLE_LABELS = {
    "title",
    "doc_title",
    "document_title",
    "paragraph_title",
    "section_title",
    "section_header",
    "heading",
}
_IMAGE_LABELS = {"image", "figure", "picture", "chart", "seal"}
_TABLE_LABELS = {"table", "table_body"}
_CAPTION_LABELS = {
    "figure_title",
    "figure_caption",
    "image_caption",
    "table_title",
    "table_caption",
}


def _element_type(role: str, block: dict[str, Any]) -> ElementType:
    if role in _CAPTION_LABELS:
        return ElementType.TEXT
    title_level = block.get("title_level")
    if isinstance(title_level, int) and title_level > 0:
        return ElementType.TITLE
    if role in _TITLE_LABELS or role.endswith("_title"):
        return ElementType.TITLE
    if role in _IMAGE_LABELS:
        return ElementType.IMAGE
    if role in _TABLE_LABELS:
        return ElementType.TABLE
    return ElementType.TEXT


def _positive_heading_level(block: dict[str, Any]) -> int | None:
    value = block.get("title_level")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _document_orientation_angle(raw: object) -> int | None:
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    result = raw.get("res", raw)
    if not isinstance(result, dict):
        return None
    preprocessor = result.get("doc_preprocessor_res")
    if not isinstance(preprocessor, dict):
        return None
    if "angle" not in preprocessor:
        return None
    value = preprocessor.get("angle")
    valid_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    valid_float = not isinstance(value, float) or (
        math.isfinite(value) and value.is_integer()
    )
    if not valid_number or not valid_float or int(value) not in {0, 90, 180, 270}:
        raise PaddleAdapterError(
            "Paddle returned an invalid document orientation angle; "
            "expected one of 0, 90, 180, or 270"
        )
    return int(value)


def _block_text(block: dict[str, Any]) -> str:
    for key in ("block_content", "text", "content", "markdown", "html"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_text_lines(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _split_embedded_titles(value: str) -> list[tuple[ElementType, str]]:
    segments: list[tuple[ElementType, str]] = []
    body: list[str] = []

    def flush_body() -> None:
        if body:
            segments.append((ElementType.TEXT, "\n".join(body)))
            body.clear()

    for line in value.splitlines():
        if _looks_like_embedded_title(line):
            flush_body()
            segments.append((ElementType.TITLE, line.strip()))
        else:
            body.append(line)
    flush_body()
    return segments or [(ElementType.TEXT, value)]


def _looks_like_embedded_title(value: str) -> bool:
    clean = " ".join(value.split())
    if not clean or len(clean) > 120 or len(clean.split()) > 12:
        return False
    if "..." in clean or clean.endswith((".", ";", ",")):
        return False
    clean = clean.rstrip(":").strip()
    letters = [character for character in clean if character.isalpha()]
    if len(letters) < 2:
        return False
    numbered = bool(re.match(r"^\d+(?:[.\-_]\d+)*\b", clean))
    uppercase_ratio = sum(character.isupper() for character in letters) / len(letters)
    return numbered or uppercase_ratio >= 0.75


def _rendered_page_size(page: _RenderedPage) -> PageSize:
    dimensions = (page.width, page.height)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        for value in dimensions
    ):
        raise PaddleAdapterError(
            f"Paddle source page {page.page} has invalid rendered dimensions: "
            f"{page.width}x{page.height}"
        )
    return PageSize(width=float(page.width), height=float(page.height))


def _corrected_page_size(page_size: PageSize, orientation_angle: int) -> PageSize:
    _validate_orientation_angle(orientation_angle)
    if orientation_angle in {90, 270}:
        return PageSize(width=page_size.height, height=page_size.width)
    return page_size


def _validate_orientation_angle(orientation_angle: int) -> None:
    if isinstance(orientation_angle, bool) or orientation_angle not in {0, 90, 180, 270}:
        raise PaddleAdapterError(
            "Paddle bbox geometry has an invalid orientation angle; "
            "expected one of 0, 90, 180, or 270"
        )


def _block_bbox(
    block: dict[str, Any],
    page_size: PageSize,
    *,
    orientation_angle: int = 0,
) -> BoundingBox | None:
    value = block.get("block_bbox", block.get("bbox", block.get("coordinate")))
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(coordinate) for coordinate in (x0, y0, x1, y1)):
        return None
    corrected_size = _corrected_page_size(page_size, orientation_angle)
    clamped_x0 = min(corrected_size.width, max(0.0, x0))
    clamped_x1 = min(corrected_size.width, max(0.0, x1))
    clamped_y0 = min(corrected_size.height, max(0.0, y0))
    clamped_y1 = min(corrected_size.height, max(0.0, y1))
    left, right = sorted((clamped_x0, clamped_x1))
    top, bottom = sorted((clamped_y0, clamped_y1))
    if right - left <= 1 or bottom - top <= 1:
        return None
    corrected_bbox = BoundingBox(x0=left, y0=top, x1=right, y1=bottom)
    return _bbox_from_corrected_frame(
        corrected_bbox,
        page_size=page_size,
        orientation_angle=orientation_angle,
    )


def _bbox_from_corrected_frame(
    bbox: BoundingBox,
    *,
    page_size: PageSize,
    orientation_angle: int,
) -> BoundingBox:
    """Map Paddle's post-rotation bbox back onto the rendered source image.

    PaddleX passes the predicted positive angle to OpenCV, so its corrected
    frame is counter-clockwise from the source frame. Quarter turns also swap
    the corrected frame's width and height.
    """

    _validate_orientation_angle(orientation_angle)
    if orientation_angle == 0:
        return bbox
    if orientation_angle == 90:
        coordinates = (
            page_size.width - bbox.y1,
            bbox.x0,
            page_size.width - bbox.y0,
            bbox.x1,
        )
    elif orientation_angle == 180:
        coordinates = (
            page_size.width - bbox.x1,
            page_size.height - bbox.y1,
            page_size.width - bbox.x0,
            page_size.height - bbox.y0,
        )
    else:  # 270 degrees
        coordinates = (
            bbox.y0,
            page_size.height - bbox.x1,
            bbox.y1,
            page_size.height - bbox.x0,
        )
    return BoundingBox(
        x0=coordinates[0],
        y0=coordinates[1],
        x1=coordinates[2],
        y1=coordinates[3],
    )


def _block_sort_key(block: dict[str, Any], index: int) -> tuple[int, float, float, int]:
    order = block.get("block_order", block.get("order"))
    if isinstance(order, int):
        return (0, float(order), 0.0, index)
    bbox = block.get("block_bbox", block.get("bbox"))
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        try:
            return (1, float(bbox[1]), float(bbox[0]), index)
        except (TypeError, ValueError):
            pass
    return (2, float(index), 0.0, index)


def _save_crop(
    page_path: Path,
    bbox: BoundingBox,
    target: Path,
    assets_root: Path,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(page_path) as image:
            crop = image.crop((bbox.x0, bbox.y0, bbox.x1, bbox.y1))
            crop.save(target)
    except Exception as exc:
        raise PaddleAdapterError(f"Cannot create crop {target.name}: {exc}") from exc
    try:
        return target.relative_to(assets_root.parent).as_posix()
    except ValueError as exc:
        raise PaddleAdapterError("assets_dir must be contained in a run directory") from exc


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaddleAdapterError(f"Cannot read Paddle worker response: {exc}") from exc
    if not isinstance(value, dict):
        raise PaddleAdapterError("Paddle worker response must be a JSON object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
