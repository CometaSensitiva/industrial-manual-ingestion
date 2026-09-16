from __future__ import annotations

from io import StringIO

import pytest

from manual_ingestion.cli_progress import RichProgressRenderer
from manual_ingestion.progress import (
    ProgressEvent,
    ProgressStage,
    ProgressState,
    emit_progress,
)


class NonTtyBuffer(StringIO):
    def isatty(self) -> bool:
        return False


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_contract_exposes_only_the_final_stage_and_state_vocabulary() -> None:
    assert [stage.value for stage in ProgressStage] == [
        "preparation",
        "detection",
        "extraction",
        "crop_filter",
        "enrichment",
        "table_serialization",
        "validation",
        "publication",
    ]
    assert [state.value for state in ProgressState] == [
        "started",
        "updated",
        "completed",
        "failed",
    ]


def test_progress_event_is_immutable_and_accepts_canonical_string_values() -> None:
    event = ProgressEvent(
        stage="extraction",  # type: ignore[arg-type]
        state="updated",  # type: ignore[arg-type]
        current=6,
        total=34,
        unit="pagine",
        detail="Batch Paddle completato",
    )

    assert event.stage is ProgressStage.EXTRACTION
    assert event.state is ProgressState.UPDATED
    with pytest.raises((AttributeError, TypeError)):
        event.current = 12  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("current", -1, ValueError),
        ("total", -1, ValueError),
        ("current", 1.5, TypeError),
        ("total", True, TypeError),
        ("unit", " pagine", ValueError),
        ("detail", "", ValueError),
    ],
)
def test_progress_event_rejects_ambiguous_values(field, value, error) -> None:
    values = {"stage": "extraction", "state": "updated", field: value}
    with pytest.raises(error):
        ProgressEvent(**values)  # type: ignore[arg-type]


def test_progress_event_rejects_current_above_total() -> None:
    with pytest.raises(ValueError, match="current cannot exceed total"):
        ProgressEvent("enrichment", "updated", current=3, total=2)  # type: ignore[arg-type]


def test_emit_progress_is_a_noop_without_callback_and_delivers_the_same_event() -> None:
    received: list[ProgressEvent] = []
    event = ProgressEvent("preparation", "started")  # type: ignore[arg-type]

    emit_progress(None, event)
    emit_progress(received.append, event)

    assert received == [event]


def test_progress_callback_failure_cannot_change_the_ingestion_outcome() -> None:
    event = ProgressEvent("publication", "completed")  # type: ignore[arg-type]

    def broken_renderer(_event: ProgressEvent) -> None:
        raise RuntimeError("terminal disappeared")

    emit_progress(broken_renderer, event)


def test_renderer_is_silent_by_default_for_non_tty_streams() -> None:
    stream = NonTtyBuffer()
    renderer = RichProgressRenderer(stream=stream)

    with renderer:
        renderer(ProgressEvent("preparation", "started"))  # type: ignore[arg-type]
        renderer(ProgressEvent("preparation", "completed"))  # type: ignore[arg-type]

    assert not renderer.enabled
    assert not renderer.active
    assert stream.getvalue() == ""


def test_renderer_uses_indeterminate_display_without_inventing_a_counter() -> None:
    stream = TtyBuffer()

    with RichProgressRenderer(stream=stream, refresh_per_second=20) as renderer:
        renderer(
            ProgressEvent(
                "extraction",  # type: ignore[arg-type]
                "started",  # type: ignore[arg-type]
                detail="Docling",
            )
        )
        renderer(
            ProgressEvent(
                "extraction",  # type: ignore[arg-type]
                "completed",  # type: ignore[arg-type]
                detail="Docling",
            )
        )
        task = renderer._progress.tasks[0]
        assert task.fields["label"] == "✓ Extracting — Docling"
        assert task.fields["counter"] == ""

    assert renderer._progress.live.transient is True


def test_renderer_switches_to_a_determinate_real_counter_and_marks_completion() -> None:
    stream = TtyBuffer()

    renderer = RichProgressRenderer(stream=stream)
    with renderer:
        renderer(
            ProgressEvent(
                "extraction",  # type: ignore[arg-type]
                "started",  # type: ignore[arg-type]
                current=0,
                total=34,
                unit="pagine",
            )
        )
        updated = ProgressEvent(
            "extraction",  # type: ignore[arg-type]
            "updated",  # type: ignore[arg-type]
            current=6,
            total=34,
            unit="pagine",
        )
        renderer(updated)
        task = renderer._progress.tasks[0]
        assert task.completed == 6
        assert task.total == 34
        assert task.fields["counter"] == "6/34 pagine"
        renderer(
            ProgressEvent(
                "extraction",  # type: ignore[arg-type]
                "completed",  # type: ignore[arg-type]
                current=34,
                total=34,
                unit="pagine",
            )
        )
        task = renderer._progress.tasks[0]
        assert task.fields["counter"] == "34/34 pagine"
        assert task.fields["label"] == "✓ Extracting"
    renderer.close()

    assert renderer._progress.live.transient is True
    assert not renderer.active


def test_renderer_marks_failed_stage_and_never_uses_standard_output(capsys) -> None:
    stream = TtyBuffer()

    with RichProgressRenderer(stream=stream) as renderer:
        renderer(
            ProgressEvent(
                "enrichment",  # type: ignore[arg-type]
                "failed",  # type: ignore[arg-type]
                current=2,
                total=4,
                unit="elementi",
                detail="provider non disponibile",
            )
        )
        task = renderer._progress.tasks[0]
        assert task.fields["label"] == "✗ Describing images — provider non disponibile"
        assert task.fields["counter"] == "2/4 elementi"

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert renderer._progress.live.transient is True
