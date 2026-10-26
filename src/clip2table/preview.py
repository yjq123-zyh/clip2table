"""preview: show the pasted text beside the table, and point at the source line.

The report already says *what* failed. This says *where*: for every field the
value, the line number in the original text, and the raw line itself. The
number is the whole point -- "第 7 条记录第 2 行" still makes the reader count
records by hand, and counting by hand is the manual work a preview exists to
remove.

Two views, because they answer different questions:
  - by field: one row per record, one column per field, plus the source line.
  - by line: the numbered original text, with which record claimed each line.
The second is what makes a wrong boundary visible: an orphan line between two
records is obvious there and invisible in the table.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ParseResult, Row, _nonempty


def _clip(s: str, n: int = 28) -> str:
    s = s.replace("\n", "⏎").replace("\t", "→").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _value(v: Any, n: int = 28) -> str:
    if v is None or v == "":
        return "∅"
    return _clip(str(v), n)


@dataclass
class Grid:
    """The table as text, with a source-line column that is verifiable."""

    lines: list[str]
    widths: dict[str, int]

    def render(self) -> str:
        return "\n".join(self.lines)


def grid(res: ParseResult, limit: int = 10, show_source: bool = True) -> Grid:
    """Records × fields, plus where each record started in the source text."""
    rows = res.rows[:limit]
    fields = list(res.fields)
    head = ["#"] + fields + (["原文行"] if show_source else [])
    body: list[list[str]] = []
    for i, r in enumerate(rows, 1):
        cells = [_value(r.values.get(f)) for f in fields]
        src = "" if not show_source else (
            f"{r.source_line + 1}" if r.source_line is not None else "—")
        body.append([str(i)] + cells + ([src] if show_source else []))
    widths = {h: len(h) for h in head}
    for line in body:
        for h, c in zip(head, line):
            widths[h] = max(widths[h], _w(c))
    out = ["  ".join(_pad(h, widths[h]) for h in head)]
    out.append("  ".join("-" * widths[h] for h in head))
    for line in body:
        out.append("  ".join(_pad(c, widths[h]) for h, c in zip(head, line)))
    if len(res.rows) > limit:
        out.append(f"  …还有 {len(res.rows) - limit} 条（用 --rows 调大）")
    return Grid(lines=out, widths=widths)


def _w(s: str) -> int:
    """Display width: a CJK character occupies two cells in a terminal."""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _w(s))


def numbered_source(lines: list[str], res: ParseResult, start: int = 0,
                    limit: int = 40) -> list[str]:
    """The original lines with their numbers, and who claimed each one.

    Unclaimed lines are marked `·` rather than hidden. They are the evidence a
    boundary is wrong: when the anchor is off by one, the giveaway is a line
    between two records that belongs to neither.
    """
    claimed: dict[int, str] = {}
    for i, r in enumerate(res.rows, 1):
        # Every line's own source index, not start+offset: a record with a
        # blank line inside it is not contiguous, and start+offset then marks
        # the wrong lines -- both the value's line and the one before it.
        at = r.raw_at or (
            [r.source_line + k for k in range(len(r.raw_lines))]
            if r.source_line is not None else [])
        for k in at:
            claimed[k] = f"第{i}条"
    for r in _unparsed_rows(lines, res):
        claimed.setdefault(r, "未识别")

    out: list[str] = []
    shown = 0
    for i, ln in enumerate(lines):
        if i < start:
            continue
        if not ln.strip():
            continue
        if shown >= limit:
            out.append(f"  …（后面还有 {len(lines) - i} 行，用 --lines 调大）")
            break
        who = claimed.get(i, "·")
        out.append(f"  {i + 1:>4}  {who:<6}  {_clip(ln, 60)}")
        shown += 1
    return out


def _unparsed_rows(lines: list[str], res: ParseResult) -> list[int]:
    """Best-effort positions for unparsed lines.

    Matched by text from the end, so a line that also appears inside a record
    is not counted twice. Approximate by design: the point is a visual cue for
    a human, and claiming a precision this does not have would be worse than
    leaving a line marked `·`.
    """
    if not res.unparsed:
        return []
    want = list(res.unparsed)
    out: list[int] = []
    for i in range(len(lines) - 1, -1, -1):
        if not want:
            break
        ln = lines[i]
        if not ln.strip():
            continue
        for j, w in enumerate(want):
            if w == ln:
                out.append(i)
                del want[j]
                break
    return out


def field_sources(res: ParseResult, field: str, limit: int = 10) -> list[str]:
    """For one field: value, source line, and the raw line it came from.

    This is the answer to "预览中的字段能够定位原文". The line number alone is
    not enough when several fields share one line; the raw text is what lets a
    human see the rule matched the right part of it.
    """
    out: list[str] = []
    shown = 0
    for i, r in enumerate(res.rows, 1):
        if shown >= limit:
            break
        t = r.traces.get(field)
        val = r.values.get(field)
        if _nonempty(val):
            where = _where_text(r, t)
            out.append(f"  {i:>3}. {_value(val, 34)}   ← {where}")
        else:
            why = t.describe() if t is not None else "没有溯源记录"
            out.append(f"  {i:>3}. ∅  {why}")
        shown += 1
    if len(res.rows) > limit:
        out.append(f"  …还有 {len(res.rows) - limit} 条")
    return out


def source_of(row: Row, t) -> int | None:
    """1-based source line a trace points at, or None when unknown.

    Defined once so the text output and `--json` cannot disagree about which
    line a value came from -- they did, because both recomputed it separately.
    """
    if t is None or t.line is None:
        return None
    if 0 <= t.line < len(row.raw_at):
        return row.raw_at[t.line] + 1
    if row.source_line is not None:
        return row.source_line + t.line + 1
    return None


def _where_text(row: Row, t) -> str:
    """`第 N 行：原文` when the trace has a line, else what it does have."""
    if t is None:
        return "未记录来源"
    if t.line is None:
        return "未记录行号"
    abs_line = source_of(row, t)
    text = _clip(row.raw_lines[t.line], 44) if 0 <= t.line < len(row.raw_lines) else ""
    if abs_line is None:
        return f"记录内第 {t.line + 1} 行" + (f"：{text}" if text else "")
    return f"第 {abs_line} 行" + (f"：{text}" if text else "")


def ambiguous_note(res: ParseResult) -> list[str]:
    """Records that produced nothing useful -- the candidates for a fix."""
    bad = [(i, r) for i, r in enumerate(res.rows, 1)
           if not any(_nonempty(v) for v in r.values.values())]
    if not bad:
        return []
    out = [f"⚠ {len(bad)} 条记录一个字段都没取到："]
    for i, r in bad[:3]:
        out.append(f"  第 {i} 条（原文第 {r.source_line + 1 if r.source_line is not None else '?'} 行起）")
    return out
