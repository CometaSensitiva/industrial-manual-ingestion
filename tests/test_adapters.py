from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from manual_ingestion.adapters.docling_adapter import (
    DoclingContentAdapter,
    DoclingConversionError,
    DoclingUnavailableError,
)
from manual_ingestion.adapters.pymupdf_adapter import PyMuPDFOutlineAdapter
from manual_ingestion.models import ElementType


def _pdf(path: Path, pages: int, *, title: str | None = None) -> Path:
    document = fitz.open()
    for page_number in range(1, pages + 1):
        page = document.new_page(width=200, height=100)
        page.insert_text((15, 50), f"Page {page_number}")
    if title:
        document.set_metadata({"title": title})
    document.save(path)
    document.close()
    return path


def test_pymupdf_adapter_preserves_embedded_outline(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "outlined.pdf", 3, title="Service Manual")
    with fitz.open(path) as document:
        document.set_toc(
            [
                [1, "Introduction", 1],
                [2, "Installation", 2],
                [1, "Operation", 3],
            ]
        )
        document.saveIncr()

    result = PyMuPDFOutlineAdapter().extract(path)

    assert result.pages_total == 3
    assert result.document_title == "Service Manual"
    assert [(entry.level, entry.title, entry.page) for entry in result.entries] == [
        (1, "Introduction", 1),
        (2, "Installation", 2),
        (1, "Operation", 3),
    ]
    assert all(entry.confidence == 1 for entry in result.entries)


class _FakeDoclingDocument:
    def export_to_dict(self) -> dict[str, object]:
        return {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/pictures/0"},
                    {"$ref": "#/texts/1"},
                ]
            },
            "texts": [
                {
                    "label": "section_header",
                    "children": [],
                    "content_layer": "body",
                    "text": "Installation",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "t": 90,
                                "r": 150,
                                "b": 70,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
                {
                    "label": "paragraph",
                    "children": [],
                    "content_layer": "body",
                    "text": "Tighten the fastener.",
                    "prov": [{"page_no": 2, "bbox": [10, 10, 180, 40]}],
                },
                {
                    "label": "text",
                    "children": [],
                    "content_layer": "body",
                    "parent": {"$ref": "#/pictures/0"},
                    "text": "Text internal to the picture",
                    "prov": [{"page_no": 2, "bbox": [25, 50, 100, 60]}],
                },
            ],
            "pictures": [
                {
                    "label": "picture",
                    "children": [{"$ref": "#/texts/2"}],
                    "content_layer": "body",
                    "text": "Fastener position",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {
                                "l": 20,
                                "t": 45,
                                "r": 120,
                                "b": 90,
                                "coord_origin": "TOPLEFT",
                            },
                        }
                    ],
                }
            ],
            "tables": [],
        }


class _FakeConversion:
    document = _FakeDoclingDocument()


class _FakeConverter:
    def __init__(self) -> None:
        self.source: str | None = None
        self.page_range: tuple[int, int] | None = None

    def convert(self, source: str, **kwargs: object) -> _FakeConversion:
        self.source = source
        self.page_range = kwargs.get("page_range")  # type: ignore[assignment]
        return _FakeConversion()


def test_docling_adapter_normalizes_trace_and_creates_crops(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "digital.pdf", 2)
    converter = _FakeConverter()
    assets = tmp_path / "run" / "assets"
    adapter = DoclingContentAdapter(converter_factory=lambda: converter)

    result = adapter.parse(path, [2, 1], assets)

    assert converter.source == str(path)
    assert converter.page_range == (1, 2)
    assert result.pages_processed == [1, 2]
    assert [element.type for element in result.elements] == [
        ElementType.TITLE,
        ElementType.IMAGE,
        ElementType.TEXT,
    ]
    title = result.elements[0]
    assert title.page == title.source.page == 1
    assert title.source.page_size is not None
    assert title.source.page_size.width == 200
    assert title.source.bbox is not None
    assert title.source.bbox.y0 == 10
    assert title.source.bbox.y1 == 30
    assert title.trace.role == "section_header"

    picture = result.elements[1]
    assert picture.image_path is not None
    assert picture.image_path.startswith("assets/images/")
    assert (tmp_path / "run" / picture.image_path).is_file()
    assert picture.caption_original == "Fastener position"
    assert [element.trace.reading_order for element in result.elements] == [0, 1, 2]


def test_docling_adapter_reports_actionable_missing_dependency(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "digital.pdf", 1)

    def missing_factory() -> _FakeConverter:
        raise ModuleNotFoundError("No module named 'docling'")

    adapter = DoclingContentAdapter(converter_factory=missing_factory)
    with pytest.raises(DoclingUnavailableError, match=r"pip install -e '\.\[docling\]'"):
        adapter.parse(path, [1], tmp_path / "assets")


def test_docling_adapter_rejects_untraceable_output(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "digital.pdf", 1)

    class EmptyDocument:
        def export_to_dict(self) -> dict[str, object]:
            return {"texts": [{"label": "paragraph", "text": "No provenance"}]}

    class EmptyConverter:
        def convert(self, source: str, **kwargs: object) -> EmptyDocument:
            return EmptyDocument()

    adapter = DoclingContentAdapter(converter_factory=EmptyConverter)
    with pytest.raises(DoclingConversionError, match="no traceable content"):
        adapter.parse(path, [1], tmp_path / "assets")


def test_docling_adapter_builds_markdown_from_exported_table_grid(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "digital.pdf", 1)

    class TableDocument:
        def export_to_dict(self) -> dict[str, object]:
            table = {
                "label": "table",
                "children": [],
                "content_layer": "body",
                "prov": [{"page_no": 1, "bbox": [10, 10, 190, 90]}],
                "data": {
                    "grid": [
                        [{"text": "Code"}, {"text": "Meaning"}],
                        [{"text": "A|1"}, {"text": "Start"}],
                    ]
                },
            }
            return {"body": {"children": [{"$ref": "#/tables/0"}]}, "tables": [table]}

    class TableConverter:
        def convert(self, source: str, **kwargs: object) -> TableDocument:
            return TableDocument()

    result = DoclingContentAdapter(converter_factory=TableConverter).parse(
        path,
        [1],
        tmp_path / "run" / "assets",
    )

    assert len(result.elements) == 1
    table = result.elements[0]
    assert table.type is ElementType.TABLE
    assert table.table_markdown == (
        "| Code | Meaning |\n| --- | --- |\n| A\\|1 | Start |"
    )


def test_docling_public_config_records_output_affecting_crop_scale() -> None:
    assert DoclingContentAdapter(crop_scale=2.5).public_config() == {
        "crop_scale": 2.5
    }
