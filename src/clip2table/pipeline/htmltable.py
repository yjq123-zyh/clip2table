"""HTML -> lines, using html.parser only.

Tables become tab-separated lines, so a copied web table and a copied Excel
range take the exact same downstream path. No lxml, no bs4.

Why this module keeps a grid and not just strings
-------------------------------------------------
A table row is a *positional* fact: "the third cell" only means something if
the first two cells still occupy their slots. The first version of this file
joined the cells with tabs and then called `.strip()` on the result, which
deletes a leading tab -- and a leading tab is the only thing saying "the first
column is empty". A three-column table whose first cell was blank came out as

    \\t甲\\t乙   ->  甲\\t乙

so `cell col=1` returned 甲, which had moved from column 2 to column 1. The
table still had the right number of characters and no error anywhere; every
value was simply attributed to the wrong column.

So the parse now produces `list[list[str]]` first, empty cells included, and the
tab-joined lines are derived from it. `rowspan`/`colspan` are expanded into
placeholders: the spanned-away slots stay present but empty, because keeping
them present is what keeps the later columns aligned. A `<br>` inside a cell is
a line break *within* that cell and is preserved in the grid.
"""
from __future__ import annotations

from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "dd", "dt"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


def _clean_cell(text: str) -> str:
    """Normalize one cell: trim each visual line, drop blank edges, keep breaks."""
    parts = [p.strip() for p in text.split("\n")]
    while parts and not parts[0]:
        parts.pop(0)
    while parts and not parts[-1]:
        parts.pop()
    return "\n".join(parts)


class _TableText(HTMLParser):
    """Collect the document as an ordered list of text lines and table rows.

    `blocks` keeps the interleaving of paragraphs and tables, because the order
    of a page is part of what the page says. Each entry is ("line", str) or
    ("row", list[str]).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, object]] = []
        self._buf: list[str] = []
        self._cell: list[str] | None = None
        self._cell_span: tuple[int, int] = (1, 1)
        self._row: list[tuple[str, int, int]] = []
        self._table: list[list[tuple[str, int, int]]] = []
        self._skip_depth = 0
        self._in_table = 0

    # -- helpers
    def _flush_line(self) -> None:
        text = "".join(self._buf).strip()
        if text:
            self.blocks.append(("line", text))
        self._buf = []

    def _flush_cell(self) -> None:
        if self._cell is not None:
            self._row.append((_clean_cell("".join(self._cell)),) + self._cell_span)
            self._cell = None
            self._cell_span = (1, 1)

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row:
            self._table.append(self._row)
            self._row = []

    def _flush_table(self) -> None:
        self._flush_row()
        if self._table:
            for row in _expand_spans(self._table):
                self.blocks.append(("row", row))
        self._table = []

    # -- parser hooks
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self._in_table += 1
            self._flush_line()
            return
        if tag == "tr" and self._in_table:
            self._flush_row()
            return
        if tag in ("td", "th") and self._in_table:
            self._flush_cell()
            self._cell = []
            d = {k.lower(): v for k, v in attrs}
            self._cell_span = (_span_attr(d.get("rowspan")),
                               _span_attr(d.get("colspan")))
            return
        if tag == "br":
            # Inside a cell the break belongs to the cell, not to the page:
            # flushing the line buffer here would drop the first half of the
            # text and concatenate the two halves into one string.
            if self._cell is not None:
                self._cell.append("\n")
            else:
                self._flush_line()
            return
        if tag in _BLOCK_TAGS:
            self._flush_line()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag in ("td", "th") and self._in_table:
            self._flush_cell()
            return
        if tag == "tr" and self._in_table:
            self._flush_row()
            return
        if tag == "table":
            self._flush_table()
            self._in_table = max(0, self._in_table - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush_line()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush_table()
        self._flush_line()


def _span_attr(raw: str | None) -> int:
    """rowspan/colspan: 0 or nonsense means 1, never "swallow the rest"."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _expand_spans(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """Turn (text, rowspan, colspan) cells into a rectangular grid of strings.

    A cell that spans two rows appears in both rows; a cell that spans two
    columns occupies two slots with the text in the first and "" in the rest.
    Both are placeholders on purpose: dropping the covered slots would shift
    every later column left, which is the defect this function exists to fix.
    """
    grid: list[list[str]] = []
    carry: dict[int, list] = {}          # column -> [text, rows still to cover]
    for cells in rows:
        out: list[str] = []
        col = 0
        idx = 0
        while True:
            if col in carry:
                text, left = carry[col]
                out.append(text)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col] = [text, left - 1]
                col += 1
                continue
            if idx < len(cells):
                text, rspan, cspan = cells[idx]
                idx += 1
                for k in range(cspan):
                    out.append(text if k == 0 else "")
                    if rspan > 1:
                        carry[col] = [text if k == 0 else "", rspan - 1]
                    col += 1
                continue
            # No more cells in this row: pad up to the next carried column so
            # the remaining span slots land at their true column index.
            nxt = min((c for c in carry if c > col), default=None)
            if nxt is None:
                break
            out.extend([""] * (nxt - col))
            col = nxt
        grid.append(out)
    return grid


def html_to_blocks(html: str) -> list[tuple[str, object]]:
    """The document as ordered blocks: ("line", str) or ("row", list[str])."""
    p = _TableText()
    p.feed(html)
    p.close()
    return p.blocks


def html_to_grid(html: str) -> list[list[str]]:
    """Every table row as a list of cells, empty cells and span slots included."""
    return [list(block) for kind, block in html_to_blocks(html) if kind == "row"]


def html_to_lines(html: str) -> list[str]:
    """Convert HTML to plain text lines; table rows become tab-joined lines.

    Empty cells are preserved exactly where they were, including a leading one,
    because `cell col=N` indexes into this split and a trimmed tab silently
    moves every later value one column to the left.

    An intra-cell line break (a `<br>` inside a `<td>`) is flattened to a space
    here. That is a deliberate loss: this function's contract is one row per
    line, and a newline inside a cell would split one record into two. The grid
    returned by `html_to_grid` keeps the real break for callers that can use it.
    """
    return [text for _kind, text in html_line_blocks(html)]


def html_line_blocks(html: str) -> list[tuple[str, str]]:
    """The same flattened text as html_to_lines, retaining table-row membership."""
    out: list[tuple[str, str]] = []
    for kind, block in html_to_blocks(html):
        if kind == "line":
            text = str(block)
            if text:
                out.append((kind, text))
            continue
        cells = [(" ".join(c.split("\n")) if "\n" in c else c) for c in block]
        if any(c for c in cells):
            out.append((kind, "\t".join(cells)))
    return out
