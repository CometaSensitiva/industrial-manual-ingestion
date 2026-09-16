"""Deterministic, parser-independent serialization of canonical table elements."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
from collections.abc import Sequence

from .models import (
    Chapter,
    ElementType,
    ManualDocument,
    ManualElement,
    TableSerialization,
    TableSerializationStatus,
    TableSerializedRow,
)

TABLE_SERIALIZATION_STRATEGY = "header_value_rows_v1"

_WHITESPACE = re.compile(r"\s+")
_MARKDOWN_SEPARATOR = re.compile(r"^:?-{3,}:?$")


@dataclass(frozen=True)
class _ParsedTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _HTMLCell:
    value: str
    is_header: bool
    colspan: int
    rowspan: int


@dataclass(frozen=True)
class _ExpandedRow:
    values: tuple[str, ...]
    header_flags: tuple[bool, ...]


@dataclass(frozen=True)
class _ActiveSpan:
    value: str
    is_header: bool
    remaining_rows: int


def serialize_manual_tables(manual: ManualDocument) -> ManualDocument:
    """Return a deep copy with every table serialized; never mutate ``manual``."""

    result = manual.model_copy(deep=True)

    def walk(items: list[Chapter | ManualElement], chapter_path: tuple[str, ...]) -> None:
        for item in items:
            if isinstance(item, Chapter):
                walk(item.content, (*chapter_path, item.title))
            elif item.type is ElementType.TABLE:
                item.table_serialization = serialize_table_element(
                    item,
                    manual_title=result.title,
                    chapter_path=chapter_path,
                )

    walk(result.content, ())
    return ManualDocument.model_validate(result.model_dump(mode="python"))


def serialize_table_element(
    element: ManualElement,
    *,
    manual_title: str,
    chapter_path: Sequence[str] = (),
) -> TableSerialization:
    """Build the canonical serialization for one table element."""

    if element.type is not ElementType.TABLE:
        raise ValueError("only table elements can be serialized")

    raw = element.table_markdown or ""
    normalized_raw = _normalize(raw)
    if not normalized_raw:
        return TableSerialization(status=TableSerializationStatus.UNAVAILABLE)

    parsed = _parse_table(raw)
    if parsed is None or not parsed.rows:
        return TableSerialization(
            status=TableSerializationStatus.FALLBACK,
            rows=[
                _serialized_row(
                    element=element,
                    manual_title=manual_title,
                    chapter_path=chapter_path,
                    row_index=1,
                    values=(("Contenuto", normalized_raw),),
                )
            ],
        )

    rows = [
        _serialized_row(
            element=element,
            manual_title=manual_title,
            chapter_path=chapter_path,
            row_index=index,
            values=tuple(zip(parsed.headers, values, strict=True)),
        )
        for index, values in enumerate(parsed.rows, start=1)
    ]
    return TableSerialization(
        status=TableSerializationStatus.STRUCTURED,
        rows=rows,
    )


def _serialized_row(
    *,
    element: ManualElement,
    manual_title: str,
    chapter_path: Sequence[str],
    row_index: int,
    values: Sequence[tuple[str, str]],
) -> TableSerializedRow:
    caption = _normalize(element.caption_generated or "")
    if not caption:
        caption = _normalize(element.caption_original or "") or "non disponibile"
    path = " > ".join(filter(None, (_normalize(part) for part in chapter_path)))
    lines = [
        _label("Manuale", _normalize(manual_title) or "non disponibile"),
        _label("Percorso capitolo", path or "Radice"),
        _label("Pagina PDF", str(element.page)),
        _label("Tabella", element.id),
        _label("Descrizione tabella", caption),
        _label("Riga", str(row_index)),
    ]
    lines.extend(_label(header, value) for header, value in values)
    return TableSerializedRow(
        id=f"{element.id}-row-{row_index:04d}",
        row_index=row_index,
        serialized_text="\n".join(lines),
    )


def _label(name: str, value: str) -> str:
    return f"{name}: {value}" if value else f"{name}:"


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", unescape(value)).strip()


def _parse_table(raw: str) -> _ParsedTable | None:
    if re.search(r"<\s*table(?:\s|>)", raw, flags=re.IGNORECASE):
        return _parse_html_table(raw)
    return _parse_markdown_table(raw)


def _parse_markdown_table(raw: str) -> _ParsedTable | None:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    parsed_rows: list[list[str]] = []
    separator_index: int | None = None
    for line in lines:
        cells, table_like = _split_markdown_row(line)
        if not table_like:
            return None
        if cells and all(_MARKDOWN_SEPARATOR.fullmatch(cell.strip()) for cell in cells):
            if not parsed_rows or separator_index is not None:
                return None
            separator_index = len(parsed_rows)
        parsed_rows.append([_normalize(cell) for cell in cells])

    if not parsed_rows:
        return None
    if separator_index is not None:
        if separator_index != 1:
            return None
        header = parsed_rows[0]
        body = parsed_rows[2:]
        body = [row for row in body if any(row)]
        if body:
            return _finalize_table(header, body)
        return _finalize_table(
            [f"Colonna {index}" for index in range(1, len(header) + 1)],
            [header],
        )

    if len(parsed_rows) == 1:
        row = parsed_rows[0]
        return _finalize_table(
            [f"Colonna {index}" for index in range(1, len(row) + 1)],
            [row],
        )
    return None


def _split_markdown_row(line: str) -> tuple[list[str], bool]:
    stripped = line.strip()
    starts_with_pipe = stripped.startswith("|")
    ends_with_pipe = _ends_with_unescaped_pipe(stripped)
    if starts_with_pipe:
        stripped = stripped[1:]
    if ends_with_pipe and stripped:
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    separator_count = 0
    index = 0
    while index < len(stripped):
        character = stripped[index]
        if character == "\\" and index + 1 < len(stripped):
            following = stripped[index + 1]
            if following in {"|", "\\"}:
                current.append(following)
                index += 2
                continue
        if character == "|":
            cells.append("".join(current))
            current = []
            separator_count += 1
        else:
            current.append(character)
        index += 1
    cells.append("".join(current))
    return cells, starts_with_pipe or ends_with_pipe or separator_count > 0


def _ends_with_unescaped_pipe(value: str) -> bool:
    if not value.endswith("|"):
        return False
    backslashes = 0
    for character in reversed(value[:-1]):
        if character != "\\":
            break
        backslashes += 1
    return backslashes % 2 == 0


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_HTMLCell]] = []
        self._table_depth = 0
        self._completed = False
        self._row: list[_HTMLCell] | None = None
        self._cell_tag: str | None = None
        self._cell_parts: list[str] = []
        self._cell_colspan = 1
        self._cell_rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._completed and self._table_depth == 0:
                return
            self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._finish_cell()
            attributes = {key.lower(): value for key, value in attrs}
            self._cell_tag = tag
            self._cell_parts = []
            self._cell_colspan = _parse_span(attributes.get("colspan"))
            self._cell_rowspan = _parse_span(attributes.get("rowspan"))
        elif tag == "br" and self._cell_tag is not None:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table" and self._table_depth:
            if self._table_depth == 1:
                self._finish_row()
                self._completed = True
            self._table_depth -= 1
            return
        if self._table_depth != 1:
            return
        if tag in {"th", "td"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._cell_tag is not None:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._table_depth:
            self._finish_row()

    def _finish_cell(self) -> None:
        if self._cell_tag is None or self._row is None:
            return
        self._row.append(
            _HTMLCell(
                value=_normalize("".join(self._cell_parts)),
                is_header=self._cell_tag == "th",
                colspan=self._cell_colspan,
                rowspan=self._cell_rowspan,
            )
        )
        self._cell_tag = None
        self._cell_parts = []

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None


def _parse_span(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def _parse_html_table(raw: str) -> _ParsedTable | None:
    parser = _TableHTMLParser()
    parser.feed(raw)
    parser.close()
    expanded = _expand_html_rows(parser.rows)
    if not expanded:
        return None

    non_empty = [row for row in expanded if any(row.values)]
    if not non_empty:
        return None
    if len(non_empty) == 1:
        row = non_empty[0].values
        return _finalize_table(
            [f"Colonna {index}" for index in range(1, len(row) + 1)],
            [list(row)],
        )

    leading_header_rows = 0
    for row in non_empty:
        if any(row.header_flags):
            leading_header_rows += 1
        else:
            break

    if leading_header_rows and leading_header_rows < len(non_empty):
        width = max(len(row.values) for row in non_empty)
        headers: list[str] = []
        for column in range(width):
            fragments: list[str] = []
            for row in non_empty[:leading_header_rows]:
                value = row.values[column] if column < len(row.values) else ""
                if value and value not in fragments:
                    fragments.append(value)
            headers.append(" / ".join(fragments))
        body = [list(row.values) for row in non_empty[leading_header_rows:]]
        return _finalize_table(headers, body)

    if leading_header_rows == len(non_empty):
        width = max(len(row.values) for row in non_empty)
        combined = [
            " / ".join(
                dict.fromkeys(
                    row.values[column]
                    for row in non_empty
                    if column < len(row.values) and row.values[column]
                )
            )
            for column in range(width)
        ]
        return _finalize_table(
            [f"Colonna {index}" for index in range(1, width + 1)],
            [combined],
        )

    return _finalize_table(
        list(non_empty[0].values),
        [list(row.values) for row in non_empty[1:]],
    )


def _expand_html_rows(rows: Sequence[Sequence[_HTMLCell]]) -> list[_ExpandedRow]:
    expanded: list[_ExpandedRow] = []
    active: dict[int, _ActiveSpan] = {}
    for source_row in rows:
        values: dict[int, str] = {}
        header_flags: dict[int, bool] = {}
        next_active: dict[int, _ActiveSpan] = {}
        for column, span in active.items():
            values[column] = span.value
            header_flags[column] = span.is_header
            if span.remaining_rows > 1:
                next_active[column] = _ActiveSpan(
                    span.value,
                    span.is_header,
                    span.remaining_rows - 1,
                )

        column = 0
        for cell in source_row:
            while any(position in values for position in range(column, column + cell.colspan)):
                column += 1
            for position in range(column, column + cell.colspan):
                values[position] = cell.value
                header_flags[position] = cell.is_header
                if cell.rowspan > 1:
                    next_active[position] = _ActiveSpan(
                        cell.value,
                        cell.is_header,
                        cell.rowspan - 1,
                    )
            column += cell.colspan

        active = next_active
        if not values:
            continue
        width = max(values) + 1
        expanded.append(
            _ExpandedRow(
                values=tuple(values.get(index, "") for index in range(width)),
                header_flags=tuple(header_flags.get(index, False) for index in range(width)),
            )
        )
    return expanded


def _finalize_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> _ParsedTable | None:
    non_empty_rows = [[_normalize(value) for value in row] for row in rows]
    non_empty_rows = [row for row in non_empty_rows if any(row)]
    if not non_empty_rows:
        return None
    width = max(len(headers), *(len(row) for row in non_empty_rows))
    canonical_headers = _canonical_headers(list(headers), width)
    padded_rows = [tuple([*row, *([""] * (width - len(row)))]) for row in non_empty_rows]
    return _ParsedTable(headers=tuple(canonical_headers), rows=tuple(padded_rows))


def _canonical_headers(headers: list[str], width: int) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for index in range(1, width + 1):
        candidate = _normalize(headers[index - 1]) if index <= len(headers) else ""
        candidate = candidate or f"Colonna {index}"
        unique = candidate
        if unique.casefold() in seen:
            unique = f"{candidate} [colonna {index}]"
            suffix = 2
            while unique.casefold() in seen:
                unique = f"{candidate} [colonna {index}.{suffix}]"
                suffix += 1
        canonical.append(unique)
        seen.add(unique.casefold())
    return canonical
