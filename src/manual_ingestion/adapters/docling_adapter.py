"""Docling content adapter for digital PDFs.

The optional dependency is isolated here: importing the V2 package never imports
Docling.  A converter factory can be injected for deterministic tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import fitz

from manual_ingestion.models import (
    BoundingBox,
    ElementType,
    ManualElement,
    PageSize,
    SourceReference,
    TraceMetadata,
)
from manual_ingestion.profiles.base import ContentParseResult


class DoclingUnavailableError(RuntimeError):
    """Raised when the selected Docling adapter is not installed."""


class DoclingConversionError(RuntimeError):
    """Raised when Docling cannot return traceable canonical content."""


class _DoclingConverter(Protocol):
    def convert(self, source: str, **kwargs: object) -> object:
        """Convert a PDF and return a Docling conversion result."""


ConverterFactory = Callable[[], _DoclingConverter]

_TITLE_LABELS = {"title", "section_header", "heading"}
_IMAGE_LABELS = {"picture", "image", "figure"}


class DoclingContentAdapter:
    """Convert Docling's exported layout into V2 ``ManualElement`` objects."""

    name = "docling"

    def __init__(
        self,
        *,
        converter_factory: ConverterFactory | None = None,
        crop_scale: float = 2.0,
    ) -> None:
        if crop_scale <= 0:
            raise ValueError("crop_scale must be positive")
        self._converter_factory = converter_factory
        self._crop_scale = crop_scale

    def public_config(self) -> dict[str, float]:
        """Expose the output-affecting adapter setup without runtime internals."""

        return {"crop_scale": self._crop_scale}

    def parse(
        self,
        pdf_path: Path,
        pages: list[int],
        assets_dir: Path,
    ) -> ContentParseResult:
        path = Path(pdf_path)
        selected_pages = _validate_pages(path, pages)
        converter = self._create_converter()
        raw_documents = _convert_selected_ranges(converter, path, selected_pages)

        assets_root = Path(assets_dir)
        elements: list[ManualElement] = []
        warnings: list[str] = []
        type_counters = {element_type: 0 for element_type in ElementType}

        try:
            with fitz.open(path) as source_pdf:
                raw_index = 0
                for raw_document in raw_documents:
                    for node in _ordered_nodes(raw_document):
                        raw_index += 1
                        provenance = _selected_provenance(node, selected_pages)
                        if provenance is None:
                            warnings.append(
                                f"Docling node {raw_index} skipped: no provenance on selected pages"
                            )
                            continue

                        page_number = _page_number(provenance)
                        page = source_pdf.load_page(page_number - 1)
                        page_size = PageSize(
                            width=float(page.rect.width), height=float(page.rect.height)
                        )
                        bbox = _bbox_from_provenance(provenance, page_size)
                        element_type = _element_type(node)
                        text = _node_text(node)

                        if element_type in {ElementType.TITLE, ElementType.TEXT} and not text:
                            warnings.append(
                                f"Docling node {raw_index} skipped: empty {element_type.value} node"
                            )
                            continue

                        type_counters[element_type] += 1
                        element_id = (
                            f"docling-{element_type.value}-{page_number:04d}-"
                            f"{type_counters[element_type]:04d}"
                        )
                        role = _node_label(node)
                        source = SourceReference(page=page_number, page_size=page_size, bbox=bbox)
                        common: dict[str, Any] = {
                            "id": element_id,
                            "type": element_type,
                            "page": page_number,
                            "source": source,
                            "trace": TraceMetadata(reading_order=len(elements), role=role or None),
                        }

                        if element_type is ElementType.IMAGE:
                            image_path = _save_crop(
                                page=page,
                                bbox=bbox,
                                target=assets_root / "images" / f"{element_id}.png",
                                assets_root=assets_root,
                                scale=self._crop_scale,
                            )
                            if image_path is None:
                                warnings.append(
                                    f"{element_id} has no usable bounding box; image crop not created"
                                )
                            elements.append(
                                ManualElement(
                                    **common,
                                    image_path=image_path,
                                    caption_original=text or None,
                                )
                            )
                        elif element_type is ElementType.TABLE:
                            table_image_path = _save_crop(
                                page=page,
                                bbox=bbox,
                                target=assets_root / "tables" / f"{element_id}.png",
                                assets_root=assets_root,
                                scale=self._crop_scale,
                            )
                            if table_image_path is None:
                                warnings.append(
                                    f"{element_id} has no usable bounding box; table crop not created"
                                )
                            elements.append(
                                ManualElement(
                                    **common,
                                    table_image_path=table_image_path,
                                    table_markdown=_table_markdown(node),
                                )
                            )
                        else:
                            elements.append(ManualElement(**common, text=text))
        except DoclingConversionError:
            raise
        except Exception as exc:
            raise DoclingConversionError(f"Cannot normalize Docling output: {exc}") from exc

        if not elements:
            details = f" ({'; '.join(warnings[:3])})" if warnings else ""
            raise DoclingConversionError(
                "Docling returned no traceable content for the selected pages" + details
            )

        return ContentParseResult(
            elements=elements,
            pages_processed=selected_pages,
            warnings=warnings,
        )

    def _create_converter(self) -> _DoclingConverter:
        try:
            return self._converter_factory() if self._converter_factory else _build_docling_converter()
        except (ImportError, ModuleNotFoundError) as exc:
            raise DoclingUnavailableError(
                "Docling is required for the digital PDF profiles. Install it in this "
                "environment with: python -m pip install -e '.[docling]'"
            ) from exc
        except DoclingUnavailableError:
            raise
        except Exception as exc:
            raise DoclingConversionError(
                "Docling could not be initialized. Verify the installed Docling version and model assets. "
                f"Original error: {exc}"
            ) from exc


def _build_docling_converter() -> _DoclingConverter:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )


def _convert_selected_ranges(
    converter: _DoclingConverter,
    path: Path,
    selected_pages: list[int],
) -> list[Mapping[str, Any]]:
    raw_documents: list[Mapping[str, Any]] = []
    for page_start, page_end in _contiguous_ranges(selected_pages):
        try:
            conversion = converter.convert(
                str(path),
                page_range=(page_start, page_end),
            )
        except Exception as exc:
            raise DoclingConversionError(
                "Docling conversion failed for a digital PDF (OCR is disabled for this profile). "
                f"Source: {path}; page range: {page_start}-{page_end}. Original error: {exc}"
            ) from exc

        document = getattr(conversion, "document", conversion)
        export_to_dict = getattr(document, "export_to_dict", None)
        if not callable(export_to_dict):
            raise DoclingConversionError(
                "Docling returned no exportable document; expected document.export_to_dict()."
            )
        try:
            raw_document = export_to_dict()
        except Exception as exc:
            raise DoclingConversionError(f"Docling export_to_dict() failed: {exc}") from exc
        if not isinstance(raw_document, Mapping):
            raise DoclingConversionError("Docling export_to_dict() must return a mapping.")
        raw_documents.append(raw_document)
    return raw_documents


def _contiguous_ranges(pages: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((start, previous))
        start = previous = page
    ranges.append((start, previous))
    return ranges


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
        with fitz.open(pdf_path) as document:
            pages_total = document.page_count
    except Exception as exc:
        raise DoclingConversionError(f"Cannot open source PDF {pdf_path}: {exc}") from exc
    if selected[-1] > pages_total:
        raise ValueError(f"page {selected[-1]} exceeds the PDF page count ({pages_total})")
    return selected


def _ordered_nodes(raw_document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Resolve Docling body references while preserving document reading order."""

    ordered: list[Mapping[str, Any]] = []
    visited_refs: set[str] = set()

    def walk(item: object) -> None:
        if not isinstance(item, Mapping):
            return
        reference = item.get("$ref")
        if isinstance(reference, str):
            if reference in visited_refs:
                return
            visited_refs.add(reference)
            target = _resolve_json_pointer(raw_document, reference)
            if isinstance(target, Mapping):
                if _is_content_node(target):
                    ordered.append(target)
                else:
                    children = target.get("children")
                    if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
                        for child in children:
                            walk(child)
            return

        if _is_content_node(item):
            ordered.append(item)
            return
        children = item.get("children")
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            for child in children:
                walk(child)

    body = raw_document.get("body")
    if isinstance(body, Mapping):
        walk(body)

    for collection_name in ("texts", "tables", "pictures"):
        collection = raw_document.get(collection_name)
        if not isinstance(collection, Sequence) or isinstance(collection, (str, bytes)):
            continue
        for index, node in enumerate(collection):
            reference = f"#/{collection_name}/{index}"
            if reference in visited_refs or not isinstance(node, Mapping):
                continue
            if _is_content_node(node) and _is_top_level_node(node):
                ordered.append(node)
    return ordered


def _is_top_level_node(node: Mapping[str, Any]) -> bool:
    parent = node.get("parent")
    if not isinstance(parent, Mapping):
        return True
    reference = parent.get("$ref")
    return reference in {None, "#/body"}


def _resolve_json_pointer(document: Mapping[str, Any], reference: str) -> object | None:
    if not reference.startswith("#/"):
        return None
    current: object = document
    for encoded_part in reference[2:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _is_content_node(node: Mapping[str, Any]) -> bool:
    if not node.get("prov"):
        return False
    if str(node.get("content_layer", "")).casefold() == "furniture":
        return False
    return _node_label(node) not in {"page_header", "page_footer", "footnote", "unspecified"}


def _selected_provenance(
    node: Mapping[str, Any],
    selected_pages: list[int],
) -> Mapping[str, Any] | None:
    provenance = node.get("prov")
    candidates: list[Mapping[str, Any]]
    if isinstance(provenance, Mapping):
        candidates = [provenance]
    elif isinstance(provenance, Sequence) and not isinstance(provenance, (str, bytes)):
        candidates = [item for item in provenance if isinstance(item, Mapping)]
    else:
        return None
    selected = set(selected_pages)
    return next((item for item in candidates if _page_number_or_none(item) in selected), None)


def _page_number_or_none(provenance: Mapping[str, Any]) -> int | None:
    raw_page = provenance.get("page_no", provenance.get("page"))
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


def _page_number(provenance: Mapping[str, Any]) -> int:
    page = _page_number_or_none(provenance)
    if page is None:
        raise DoclingConversionError("Docling provenance has no valid one-based page number")
    return page


def _bbox_from_provenance(
    provenance: Mapping[str, Any],
    page_size: PageSize,
) -> BoundingBox | None:
    raw_bbox = provenance.get("bbox")
    values: tuple[float, float, float, float] | None = None
    origin = ""
    if isinstance(raw_bbox, Mapping):
        origin = str(raw_bbox.get("coord_origin", provenance.get("coord_origin", ""))).upper()
        if all(key in raw_bbox for key in ("l", "t", "r", "b")):
            left = float(raw_bbox["l"])
            top = float(raw_bbox["t"])
            right = float(raw_bbox["r"])
            bottom = float(raw_bbox["b"])
            if "BOTTOM" in origin:
                values = (left, page_size.height - top, right, page_size.height - bottom)
            else:
                values = (left, top, right, bottom)
        elif all(key in raw_bbox for key in ("x0", "y0", "x1", "y1")):
            values = tuple(float(raw_bbox[key]) for key in ("x0", "y0", "x1", "y1"))
    elif isinstance(raw_bbox, Sequence) and not isinstance(raw_bbox, (str, bytes)) and len(raw_bbox) >= 4:
        values = tuple(float(value) for value in raw_bbox[:4])

    if values is None:
        return None
    x0, y0, x1, y1 = values
    clamped_x = (
        min(page_size.width, max(0.0, x0)),
        min(page_size.width, max(0.0, x1)),
    )
    clamped_y = (
        min(page_size.height, max(0.0, y0)),
        min(page_size.height, max(0.0, y1)),
    )
    left, right = sorted(clamped_x)
    top, bottom = sorted(clamped_y)
    if right - left <= 0.01 or bottom - top <= 0.01:
        return None
    return BoundingBox(x0=left, y0=top, x1=right, y1=bottom)


def _element_type(node: Mapping[str, Any]) -> ElementType:
    label = _node_label(node)
    if label in _TITLE_LABELS:
        return ElementType.TITLE
    if label == "table":
        return ElementType.TABLE
    if label in _IMAGE_LABELS:
        return ElementType.IMAGE
    return ElementType.TEXT


def _node_label(node: Mapping[str, Any]) -> str:
    return str(node.get("label", node.get("type", "text"))).strip().lower()


def _node_text(node: Mapping[str, Any]) -> str:
    for key in ("text", "orig", "caption"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _table_markdown(node: Mapping[str, Any]) -> str | None:
    for key in ("table_markdown", "markdown", "text"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = node.get("data")
    if isinstance(data, Mapping):
        grid = data.get("grid")
        if isinstance(grid, Sequence) and not isinstance(grid, (str, bytes)):
            rows: list[list[str]] = []
            for raw_row in grid:
                if not isinstance(raw_row, Sequence) or isinstance(raw_row, (str, bytes)):
                    continue
                row: list[str] = []
                for raw_cell in raw_row:
                    text = raw_cell.get("text") if isinstance(raw_cell, Mapping) else raw_cell
                    row.append(_markdown_cell(str(text or "")))
                if row:
                    rows.append(row)
            if rows:
                width = max(len(row) for row in rows)
                normalized = [row + [""] * (width - len(row)) for row in rows]
                lines = ["| " + " | ".join(row) + " |" for row in normalized]
                lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
                return "\n".join(lines)
    return None


def _markdown_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _save_crop(
    *,
    page: fitz.Page,
    bbox: BoundingBox | None,
    target: Path,
    assets_root: Path,
    scale: float,
) -> str | None:
    if bbox is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    clip = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    pixmap.save(target)
    try:
        relative = target.relative_to(assets_root.parent)
    except ValueError as exc:
        raise DoclingConversionError("assets_dir must be contained in a run directory") from exc
    return relative.as_posix()
