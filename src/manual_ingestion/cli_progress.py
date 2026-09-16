"""TTY-aware Rich renderer for transient ingestion progress events."""

from __future__ import annotations

import sys
from types import TracebackType
from typing import TextIO

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .progress import ProgressEvent, ProgressStage, ProgressState


_STAGE_LABELS = {
    ProgressStage.PREPARATION: "Preparing",
    ProgressStage.DETECTION: "Detecting",
    ProgressStage.EXTRACTION: "Extracting",
    ProgressStage.CROP_FILTER: "Filtering images",
    ProgressStage.ENRICHMENT: "Describing images",
    ProgressStage.TABLE_SERIALIZATION: "Serializing tables",
    ProgressStage.VALIDATION: "Checking bundle",
    ProgressStage.PUBLICATION: "Saving bundle",
}


def _stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


class RichProgressRenderer:
    """Render progress on a TTY without ever writing to standard output.

    The object is directly usable as a :class:`ProgressCallback`. Unknown totals
    render as a pulsing bar with a spinner; once a real total is emitted, Rich
    automatically switches the same task to a determinate bar.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        enabled: bool | None = None,
        refresh_per_second: float = 10,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self.enabled = _stream_is_tty(self._stream) if enabled is None else enabled
        self._tasks: dict[ProgressStage, int] = {}
        self._started = False
        self._closed = False

        console = Console(file=self._stream, force_terminal=self.enabled)
        self._progress = Progress(
            SpinnerColumn(finished_text=" ", style="#b4a0e5"),
            TextColumn("{task.fields[label]}", markup=False),
            BarColumn(bar_width=20, complete_style="#b4a0e5", finished_style="green", pulse_style="#97bafa"),
            TextColumn("{task.fields[counter]}", markup=False),
            TimeElapsedColumn(),
            console=console,
            auto_refresh=True,
            refresh_per_second=refresh_per_second,
            expand=False,
            transient=True,
        )

    @property
    def active(self) -> bool:
        """Whether the Rich live display has been started and is still open."""

        return self._started and not self._closed

    def __enter__(self) -> RichProgressRenderer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __call__(self, event: ProgressEvent) -> None:
        if not self.enabled or self._closed:
            return
        self._ensure_started()

        task_id = self._tasks.get(event.stage)
        if task_id is None:
            task_id = self._progress.add_task(
                "",
                total=event.total,
                completed=event.current or 0,
                label=self._label(event),
                counter=self._counter(event),
            )
            self._tasks[event.stage] = task_id
        else:
            update: dict[str, object] = {"label": self._label(event)}
            if event.current is not None:
                update["completed"] = event.current
            if event.total is not None:
                update["total"] = event.total
            if event.current is not None or event.total is not None:
                update["counter"] = self._counter(event)
            self._progress.update(task_id, **update)

        if event.state is ProgressState.COMPLETED:
            task = self._progress.tasks[task_id]
            if event.total is not None:
                completed = event.current if event.current is not None else event.total
                self._progress.update(
                    task_id,
                    completed=completed,
                    total=event.total,
                    counter=self._format_counter(completed, event.total, event.unit),
                )
            elif task.total is not None:
                completed = event.current if event.current is not None else task.total
                self._progress.update(task_id, completed=completed)
            else:
                self._progress.update(task_id, completed=1, total=1)
            self._progress.stop_task(task_id)
        elif event.state is ProgressState.FAILED:
            self._progress.stop_task(task_id)

        self._progress.refresh()

    def close(self) -> None:
        """Stop the live display; safe to call more than once."""

        if self._started and not self._closed:
            self._progress.stop()
        self._closed = True

    def _ensure_started(self) -> None:
        if not self._started:
            self._progress.start()
            self._started = True

    @staticmethod
    def _counter(event: ProgressEvent) -> str:
        if event.current is None and event.total is None:
            return ""
        current = "?" if event.current is None else str(event.current)
        total = "?" if event.total is None else str(event.total)
        return RichProgressRenderer._format_counter(current, total, event.unit)

    @staticmethod
    def _format_counter(
        current: int | str,
        total: int | str,
        unit: str | None,
    ) -> str:
        suffix = f" {unit}" if unit else ""
        return f"{current}/{total}{suffix}"

    @staticmethod
    def _label(event: ProgressEvent) -> str:
        prefix = ""
        if event.state is ProgressState.COMPLETED:
            prefix = "✓ "
        elif event.state is ProgressState.FAILED:
            prefix = "✗ "
        detail = f" — {event.detail}" if event.detail else ""
        return f"{prefix}{_STAGE_LABELS[event.stage]}{detail}"


__all__ = ["RichProgressRenderer"]
