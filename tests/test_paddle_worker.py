from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from manual_ingestion.adapters.paddle_worker import (
    PaddleWorkerError,
    probe_runtime,
    process_request,
)


def test_probe_runtime_reports_versions_without_importing_main_package() -> None:
    class FakePaddleModule:
        class PaddleOCRVL:
            pass

    versions = {
        "paddleocr": "3.7.0",
        "paddlepaddle": "3.2.1",
        "paddlex": "3.7.2",
    }
    result = probe_runtime(
        importer=lambda name: FakePaddleModule(),
        version_reader=lambda name: versions[name],
    )

    assert result["paddleocr"] == "3.7.0"
    assert result["paddlepaddle"] == "3.2.1"
    assert result["paddlex"] == "3.7.2"
    assert result["python"]


def test_worker_processes_pages_with_one_parser_instance(tmp_path: Path) -> None:
    image_path = tmp_path / "page_0001.png"
    Image.new("RGB", (200, 300), "white").save(image_path)
    created: list[dict[str, object]] = []

    class FakeParser:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict(self, input: str, **kwargs: object):
            assert input == str(image_path)
            assert kwargs["layout_shape_mode"] == "auto"
            return [
                {
                    "res": {
                        "parsing_res_list": [
                            {
                                "block_label": "paragraph_title",
                                "block_content": "SAFETY",
                                "block_bbox": [10, 10, 190, 40],
                                "block_order": 1,
                            }
                        ]
                    }
                }
            ]

        def restructure_pages(self, results, **kwargs: object):
            assert kwargs["relevel_titles"] is True
            return results

    request = {
        "schema_version": "1.0",
        "config": {
            "pipeline_version": "v1.6",
            "vl_rec_backend": "native",
            "layout_shape_mode": "auto",
            "restructure_pages": True,
            "restructure_relevel_titles": True,
        },
        "pages": [{"page": 1, "image_path": str(image_path)}],
    }

    response = process_request(
        request,
        output_root=tmp_path / "raw",
        paddle_factory=FakeParser,
    )

    assert len(created) == 1
    assert created[0] == {"pipeline_version": "v1.6", "vl_rec_backend": "native"}
    assert response["status"] == "completed"
    assert response["pages"][0]["page"] == 1
    assert response["pages"][0]["raw"]["res"]["parsing_res_list"][0][
        "block_content"
    ] == "SAFETY"
    assert (tmp_path / "raw" / "page_0001" / "result_001.json").is_file()


def test_worker_rejects_duplicate_page_requests(tmp_path: Path) -> None:
    request = {
        "schema_version": "1.0",
        "config": {},
        "pages": [
            {"page": 1, "image_path": "/tmp/a.png"},
            {"page": 1, "image_path": "/tmp/b.png"},
        ],
    }

    with pytest.raises(PaddleWorkerError, match="unique positive"):
        process_request(
            request,
            output_root=tmp_path / "raw",
            paddle_factory=lambda **kwargs: object(),
        )
