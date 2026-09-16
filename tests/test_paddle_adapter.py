from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from manual_ingestion.adapters.paddle_adapter import (
    PaddleAdapterError,
    PaddleOCRContentAdapter,
    PaddleRuntimeConfig,
    SubprocessPaddleWorker,
    _RenderedPage,
    _block_bbox,
    _document_orientation_angle,
    _normalize_response,
)
from manual_ingestion.models import ElementType, PageSize
from manual_ingestion.progress import ProgressEvent, ProgressStage, ProgressState


class FakeRunner:
    def __init__(
        self,
        *,
        omit_last_page: bool = False,
        invalid_image_bbox: bool = False,
        embedded_heading: bool = False,
        orientation_angle: int | None = None,
    ) -> None:
        self.omit_last_page = omit_last_page
        self.invalid_image_bbox = invalid_image_bbox
        self.embedded_heading = embedded_heading
        self.orientation_angle = orientation_angle
        self.request: dict[str, object] | None = None
        self.requests: list[dict[str, object]] = []
        self.diagnostics_dirs: list[Path] = []
        self.probe_calls = 0

    def probe(self) -> dict[str, str]:
        self.probe_calls += 1
        return {
            "python": "3.12",
            "paddleocr": "3.7.0",
            "paddlepaddle": "3.2.1",
            "paddlex": "3.7.2",
        }

    def run(self, request, *, diagnostics_dir: Path, page_count: int):
        self.request = request
        self.requests.append(request)
        self.diagnostics_dirs.append(diagnostics_dir)
        pages = []
        requested = request["pages"][:-1] if self.omit_last_page else request["pages"]
        for page in requested:
            assert Path(page["image_path"]).is_file()
            width = page["width"]
            height = page["height"]
            image_bbox = [20, 140, 20, 160] if self.invalid_image_bbox else [20, 140, 120, 220]
            pages.append(
                {
                    "page": page["page"],
                    "raw": {
                        "res": {
                            "parsing_res_list": [
                                {
                                    "block_label": "header",
                                    "block_content": "Repeated header",
                                    "block_bbox": [0, 0, width, 20],
                                    "block_order": 0,
                                },
                                {
                                    "block_label": "text",
                                    "block_content": "1 SAFETY",
                                    "block_bbox": [20, 30, width - 20, 70],
                                    "block_order": 1,
                                    "title_level": 1,
                                },
                                {
                                    "block_label": "text",
                                    "block_content": "Disconnect power before service.\nUse a lockout device.",
                                    "block_bbox": [20, 80, width - 20, 130],
                                    "block_order": 2,
                                },
                                {
                                    "block_label": "image",
                                    "block_content": "Emergency stop position",
                                    "block_bbox": image_bbox,
                                    "block_order": 3,
                                },
                                {
                                    "block_label": "table",
                                    "block_content": "| Code | Meaning |\n| --- | --- |\n| A1 | Alarm |",
                                    "block_bbox": [130, 140, width - 20, 240],
                                    "block_order": 4,
                                },
                            ]
                        }
                    },
                    "markdown": "",
                }
            )
            if self.orientation_angle is not None:
                pages[-1]["raw"]["res"]["doc_preprocessor_res"] = {
                    "angle": self.orientation_angle
                }
            if self.embedded_heading:
                pages[-1]["raw"]["res"]["parsing_res_list"].append(
                    {
                        "block_label": "abstract",
                        "block_content": (
                            "Introductory paragraph.\nLIMITED WARRANTY\nWarranty body text."
                        ),
                        "block_bbox": [20, 250, width - 20, height - 20],
                        "block_order": 5,
                    }
                )
        return {
            "schema_version": "1.0",
            "status": "completed",
            "runtime": {
                "python": "3.12",
                "paddleocr": "3.7.0",
                "paddlepaddle": "3.2.1",
                "paddlex": "3.7.2",
            },
            "pages": pages,
        }


def _scanned_pdf(path: Path, pages: int = 1) -> Path:
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page(width=300, height=400)
        page.draw_rect((30, 30, 270, 370), color=(0, 0, 0))
    document.save(path)
    document.close()
    return path


def test_progress_updates_only_after_each_normalized_paddle_batch(
    tmp_path: Path,
) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=7)
    progress: list[ProgressEvent] = []
    adapter = PaddleOCRContentAdapter(
        runtime=PaddleRuntimeConfig(pages_per_worker=3),
        runner=FakeRunner(),
        progress=progress.append,
    )

    adapter.parse(source, list(range(1, 8)), tmp_path / "assets")

    assert [event.stage for event in progress] == [ProgressStage.EXTRACTION] * 3
    assert [event.state for event in progress] == [ProgressState.UPDATED] * 3
    assert [(event.current, event.total, event.unit) for event in progress] == [
        (3, 7, "pages"),
        (6, 7, "pages"),
        (7, 7, "pages"),
    ]
    assert [event.detail for event in progress] == [
        "Paddle batch 1/3",
        "Paddle batch 2/3",
        "Paddle batch 3/3",
    ]


def test_paddle_adapter_normalizes_layout_and_crops(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")
    runner = FakeRunner()
    adapter = PaddleOCRContentAdapter(runner=runner)

    result = adapter.parse(source, [1], tmp_path / "run" / "assets")

    assert result.pages_processed == [1]
    assert adapter.last_runtime == {
        "python": "3.12",
        "paddleocr": "3.7.0",
        "paddlepaddle": "3.2.1",
        "paddlex": "3.7.2",
    }
    assert [element.type for element in result.elements] == [
        ElementType.TITLE,
        ElementType.TEXT,
        ElementType.IMAGE,
        ElementType.TABLE,
    ]
    assert [element.trace.reading_order for element in result.elements] == [0, 1, 2, 3]
    assert all(element.source.page_size is not None for element in result.elements)
    assert result.elements[0].trace.role == "text"
    assert result.elements[1].text == (
        "Disconnect power before service.\nUse a lockout device."
    )
    image = result.elements[2]
    table = result.elements[3]
    assert image.image_path == "assets/images/paddle-image-0001-0001.png"
    assert table.table_image_path == "assets/tables/paddle-table-0001-0001.png"
    assert table.table_markdown == "| Code | Meaning |\n| --- | --- |\n| A1 | Alarm |"
    assert (tmp_path / "run" / image.image_path).is_file()
    assert (tmp_path / "run" / table.table_image_path).is_file()
    assert runner.request is not None
    assert runner.request["config"]["pipeline_version"] == "v1.6"
    assert (tmp_path / "run" / "assets" / "pages" / "page_0001.png").is_file()


def test_paddle_rendering_caps_oversized_scan_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "oversized.pdf"
    document = pymupdf.open()
    document.new_page(width=4000, height=3000).draw_rect(
        (20, 20, 3980, 2980), color=(0, 0, 0)
    )
    document.save(source)
    document.close()
    runner = FakeRunner()
    adapter = PaddleOCRContentAdapter(
        runtime=PaddleRuntimeConfig(max_render_edge=2000),
        runner=runner,
    )

    adapter.parse(source, [1], tmp_path / "run" / "assets")

    assert runner.request is not None
    page = runner.request["pages"][0]
    assert max(page["width"], page["height"]) == 2000


def test_paddle_adapter_isolates_long_runs_in_bounded_worker_batches(
    tmp_path: Path,
) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=7)
    runner = FakeRunner()
    adapter = PaddleOCRContentAdapter(
        runtime=PaddleRuntimeConfig(pages_per_worker=3),
        runner=runner,
    )

    result = adapter.parse(
        source,
        list(range(1, 8)),
        tmp_path / "run" / "assets",
    )

    assert [[page["page"] for page in request["pages"]] for request in runner.requests] == [
        [1, 2, 3],
        [4, 5, 6],
        [7],
    ]
    assert [path.name for path in runner.diagnostics_dirs] == [
        "batch_0001",
        "batch_0002",
        "batch_0003",
    ]
    assert result.pages_processed == list(range(1, 8))
    assert {element.page for element in result.elements} == set(range(1, 8))


def test_paddle_preflight_is_cached_and_reused_before_parse(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")
    runner = FakeRunner()
    adapter = PaddleOCRContentAdapter(runner=runner)

    first = adapter.preflight()
    second = adapter.preflight()
    adapter.parse(source, [1], tmp_path / "run" / "assets")

    assert first == second == {
        "python": "3.12",
        "paddleocr": "3.7.0",
        "paddlepaddle": "3.2.1",
        "paddlex": "3.7.2",
    }
    assert first is not second
    assert runner.probe_calls == 1


def test_paddle_public_config_records_exact_ocr_setup_without_machine_paths() -> None:
    adapter = PaddleOCRContentAdapter(
        runtime=PaddleRuntimeConfig(
            python_executable="/private/runtime/bin/python",
            cache_dir=Path("/private/cache"),
        ),
        runner=FakeRunner(),
    )

    public = adapter.public_config()

    assert public["ocr"]["pipeline_version"] == "v1.6"
    assert public["ocr"]["vl_rec_backend"] == "native"
    assert "/private" not in str(public)


def test_paddle_probe_failure_happens_before_page_rendering(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class FailingProbeRunner(FakeRunner):
        def probe(self) -> dict[str, str]:
            raise PaddleAdapterError("isolated runtime unavailable")

    run_root = tmp_path / "run"
    adapter = PaddleOCRContentAdapter(runner=FailingProbeRunner())

    with pytest.raises(PaddleAdapterError, match="runtime unavailable"):
        adapter.parse(source, [1], run_root / "assets")

    assert not (run_root / "assets" / "pages").exists()


def test_paddle_wrong_python_version_fails_before_page_rendering(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class WrongPythonRunner(FakeRunner):
        def probe(self) -> dict[str, str]:
            runtime = super().probe()
            runtime["python"] = "3.13.1"
            return runtime

    run_root = tmp_path / "run"
    adapter = PaddleOCRContentAdapter(runner=WrongPythonRunner())

    with pytest.raises(PaddleAdapterError, match="accepted scanned_ocr setup"):
        adapter.parse(source, [1], run_root / "assets")

    assert not (run_root / "assets" / "pages").exists()


def test_paddle_wrong_package_version_fails_before_page_rendering(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class WrongPackageRunner(FakeRunner):
        def probe(self) -> dict[str, str]:
            runtime = super().probe()
            runtime["paddlex"] = "3.7.3"
            return runtime

    run_root = tmp_path / "run"
    adapter = PaddleOCRContentAdapter(runner=WrongPackageRunner())

    with pytest.raises(PaddleAdapterError, match="paddlex='3.7.3'"):
        adapter.parse(source, [1], run_root / "assets")

    assert not (run_root / "assets" / "pages").exists()


def test_paddle_worker_response_must_match_preflight_runtime(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class DriftingRuntimeRunner(FakeRunner):
        def run(self, request, *, diagnostics_dir: Path, page_count: int):
            response = super().run(
                request,
                diagnostics_dir=diagnostics_dir,
                page_count=page_count,
            )
            response["runtime"].pop("paddlex")
            return response

    adapter = PaddleOCRContentAdapter(runner=DriftingRuntimeRunner())

    with pytest.raises(PaddleAdapterError, match="malformed version metadata"):
        adapter.parse(source, [1], tmp_path / "run" / "assets")


def test_paddle_adapter_recovers_heading_merged_inside_text_block(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")
    adapter = PaddleOCRContentAdapter(runner=FakeRunner(embedded_heading=True))

    result = adapter.parse(source, [1], tmp_path / "run" / "assets")

    recovered = [
        element
        for element in result.elements
        if element.type is ElementType.TITLE and element.text == "LIMITED WARRANTY"
    ]
    assert len(recovered) == 1
    assert recovered[0].trace.role == "abstract:embedded_title"


def test_paddle_adapter_promotes_short_uppercase_text_block_to_title(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class UppercaseRunner(FakeRunner):
        def run(self, request, *, diagnostics_dir: Path, page_count: int):
            response = super().run(
                request,
                diagnostics_dir=diagnostics_dir,
                page_count=page_count,
            )
            response["pages"][0]["raw"]["res"]["parsing_res_list"][2][
                "block_content"
            ] = "SPECIFICATIONS:"
            return response

    result = PaddleOCRContentAdapter(runner=UppercaseRunner()).parse(
        source,
        [1],
        tmp_path / "run" / "assets",
    )

    assert any(
        element.type is ElementType.TITLE and element.text == "SPECIFICATIONS:"
        for element in result.elements
    )


def test_paddle_adapter_keeps_figure_titles_as_text_evidence(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    class FigureTitleRunner(FakeRunner):
        def run(self, request, *, diagnostics_dir: Path, page_count: int):
            response = super().run(
                request,
                diagnostics_dir=diagnostics_dir,
                page_count=page_count,
            )
            response["pages"][0]["raw"]["res"]["parsing_res_list"].append(
                {
                    "block_label": "figure_title",
                    "block_content": "Fig. 2",
                    "block_bbox": [20, 250, 80, 270],
                    "block_order": 5,
                }
            )
            return response

    result = PaddleOCRContentAdapter(runner=FigureTitleRunner()).parse(
        source,
        [1],
        tmp_path / "run" / "assets",
    )

    figure_title = next(element for element in result.elements if element.text == "Fig. 2")
    assert figure_title.type is ElementType.TEXT
    assert figure_title.trace.role == "figure_title"


def test_paddle_adapter_preserves_positive_source_heading_level(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")

    result = PaddleOCRContentAdapter(runner=FakeRunner()).parse(
        source,
        [1],
        tmp_path / "run" / "assets",
    )

    title = next(element for element in result.elements if element.type is ElementType.TITLE)
    assert title.trace.heading_level == 1


@pytest.mark.parametrize(
    ("angle", "corrected_bbox"),
    [
        (0, [30, 40, 290, 120]),
        (90, [40, 10, 120, 270]),
        (180, [10, 80, 270, 160]),
        (270, [80, 30, 160, 290]),
    ],
)
def test_paddle_bbox_back_transform_handles_all_orientation_angles(
    angle: int,
    corrected_bbox: list[int],
) -> None:
    bbox = _block_bbox(
        {"block_bbox": corrected_bbox},
        PageSize(width=300, height=200),
        orientation_angle=angle,
    )

    assert bbox is not None
    assert (bbox.x0, bbox.y0, bbox.x1, bbox.y1) == (30, 40, 290, 120)


@pytest.mark.parametrize(
    ("corrected_bbox", "expected_source_bbox"),
    [
        ([1166, 1758, 1804, 1794], (796, 156, 1434, 192)),
        ([741, 409, 1922, 1561], (678, 389, 1859, 1541)),
    ],
)
def test_paddle_bbox_back_transform_matches_real_page_26_regression(
    corrected_bbox: list[int],
    expected_source_bbox: tuple[int, int, int, int],
) -> None:
    bbox = _block_bbox(
        {"block_bbox": corrected_bbox},
        PageSize(width=2600, height=1950),
        orientation_angle=180,
    )

    assert bbox is not None
    assert (bbox.x0, bbox.y0, bbox.x1, bbox.y1) == expected_source_bbox


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_paddle_adapter_crops_from_back_transformed_source_bbox(
    tmp_path: Path,
    angle: int,
) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")
    runner = FakeRunner(orientation_angle=angle)

    result = PaddleOCRContentAdapter(runner=runner).parse(
        source,
        [1],
        tmp_path / "run" / "assets",
    )

    assert runner.request is not None
    rendered_page = runner.request["pages"][0]
    width = rendered_page["width"]
    height = rendered_page["height"]
    expected = {
        0: (20, 140, 120, 220),
        90: (width - 220, 20, width - 140, 120),
        180: (width - 120, height - 220, width - 20, height - 140),
        270: (140, height - 120, 220, height - 20),
    }[angle]
    image = next(element for element in result.elements if element.type is ElementType.IMAGE)

    assert image.source.page_size == PageSize(width=width, height=height)
    assert image.source.bbox is not None
    assert (
        image.source.bbox.x0,
        image.source.bbox.y0,
        image.source.bbox.x1,
        image.source.bbox.y1,
    ) == expected
    with Image.open(tmp_path / "run" / image.image_path) as crop:
        assert crop.size == (expected[2] - expected[0], expected[3] - expected[1])


@pytest.mark.parametrize("angle", [45, -1, 360, "90", True, 90.5])
def test_paddle_adapter_fails_closed_on_invalid_orientation_angle(angle: object) -> None:
    raw = {"res": {"doc_preprocessor_res": {"angle": angle}}}

    with pytest.raises(PaddleAdapterError, match="invalid document orientation angle"):
        _document_orientation_angle(raw)


def test_paddle_adapter_fails_closed_on_invalid_source_dimensions(tmp_path: Path) -> None:
    response = {
        "schema_version": "1.0",
        "pages": [
            {
                "page": 1,
                "raw": {
                    "res": {
                        "parsing_res_list": [
                            {
                                "block_label": "text",
                                "block_content": "traceable text",
                                "block_bbox": [1, 1, 10, 10],
                            }
                        ]
                    }
                },
            }
        ],
    }

    with pytest.raises(PaddleAdapterError, match="invalid rendered dimensions"):
        _normalize_response(
            response,
            rendered=[
                _RenderedPage(
                    page=1,
                    path=tmp_path / "unused.png",
                    width=0,
                    height=100,
                )
            ],
            assets_root=tmp_path / "assets",
        )


def test_paddle_adapter_rejects_partial_worker_response(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=2)
    adapter = PaddleOCRContentAdapter(runner=FakeRunner(omit_last_page=True))

    with pytest.raises(PaddleAdapterError, match="missing requested pages"):
        adapter.parse(source, [1, 2], tmp_path / "run" / "assets")


def test_paddle_adapter_rejects_visual_without_traceable_bbox(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf")
    adapter = PaddleOCRContentAdapter(runner=FakeRunner(invalid_image_bbox=True))

    with pytest.raises(PaddleAdapterError, match="visual block .* no valid bbox"):
        adapter.parse(source, [1], tmp_path / "run" / "assets")


def test_subprocess_runner_reports_missing_python() -> None:
    runner = SubprocessPaddleWorker(
        PaddleRuntimeConfig(python_executable="/definitely/missing/python")
    )

    with pytest.raises(PaddleAdapterError, match="executable not found"):
        runner.probe()


def test_subprocess_runner_caps_aggregate_timeout() -> None:
    runner = SubprocessPaddleWorker(
        PaddleRuntimeConfig(
            timeout_seconds_per_page=600,
            max_total_timeout_seconds=7200,
        )
    )

    assert runner._timeout_for_pages(2) == 1200
    assert runner._timeout_for_pages(34) == 7200
