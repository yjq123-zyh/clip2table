"""assemble: turn records into rows, dedupe, and keep the books balanced.

Two invariants hold here:
  1. Conservation (L2): consumed lines + unparsed lines == total source lines.
     Catches LOST data.
  2. Per-record expectations (L4): each record must yield at least one field.
     Catches a record silently split in two - a failure mode conservation
     cannot see, because splitting keeps every line accounted for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..models import ParseResult, Row, _nonempty
from .extract import FieldSpec, extract
from .segment import Segmented


@dataclass
class AssembleOptions:
    dedupe_by: list[str] | None = None
    drop_empty: bool = True          # drop rows with every field empty
    page: int | None = None
    extra: dict[str, object] = field(default_factory=dict)


def assemble(seg: Segmented, specs: list[FieldSpec],
             opts: AssembleOptions | None = None) -> ParseResult:
    opts = opts or AssembleOptions()
    rows: list[Row] = []
    empty_records = 0
    # Lines belonging to records that produced no field at all. They move to
    # unparsed, so the conservation books still balance.
    empty_lines = 0
    unparsed = list(seg.unparsed)

    for i, rec in enumerate(seg.records):
        row = extract(rec, specs)
        row.source_page = opts.page
        # Carry the record's own offset so the preview can point at 原文. The
        # list is allowed to be short (a segmenter that did not track starts);
        # then this stays None, which the preview renders as "未记录".
        if i < len(seg.record_starts):
            row.source_line = seg.record_starts[i]
        # The whole line-by-line mapping, so a value on a line after a blank
        # one lands on the right source line instead of one short.
        if i < len(seg.record_lines):
            row.raw_at = list(seg.record_lines[i])
        if opts.drop_empty and not any(_nonempty(v) for v in row.values.values()):
            empty_records += 1
            empty_lines += len(rec)
            unparsed.extend(rec)
            continue
        rows.append(row)

    fields = [s.name for s in specs]
    consumed = seg.consumed_lines - empty_lines

    res = ParseResult(rows=rows, fields=fields, unparsed=unparsed,
                      total_lines=seg.total_lines, consumed_lines=consumed,
                      level="card", notes=list(seg.notes))
    if empty_records:
        res.notes.append(f"{empty_records} 条记录所有字段都是空的，已转为未识别原文")

    if opts.dedupe_by:
        res = dedupe(res, opts.dedupe_by, res.notes)
    return res


def dedupe(res: ParseResult, keys: Iterable[str], notes: list[str] | None = None) -> ParseResult:
    """Drop later rows whose key tuple repeats an earlier one.

    Rows with an empty key are always kept: deduping on a missing key silently
    deletes distinct records (the old script lost one of two different users
    who posted identical text for exactly this reason).
    """
    keys = list(keys)
    if not keys:
        return res
    seen: set[tuple] = set()
    kept: list[Row] = []
    dropped = 0
    empty_key = 0
    for row in res.rows:
        key = tuple((row.values.get(k) or "").strip() if isinstance(row.values.get(k), str)
                    else row.values.get(k) for k in keys)
        if not any(k not in (None, "") for k in key):
            empty_key += 1
            kept.append(row)
            continue
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(row)
    out = ParseResult(rows=kept, fields=res.fields, unparsed=res.unparsed,
                      total_lines=res.total_lines, consumed_lines=res.consumed_lines,
                      level=res.level, notes=list(res.notes))
    if notes is not None:
        notes.clear()
        notes.extend(out.notes)
    if dropped:
        out.notes.append(f"按 {','.join(keys)} 去重，丢弃 {dropped} 条重复")
    if empty_key:
        out.notes.append(f"{empty_key} 条记录去重键为空，已保留（不猜内容）")
    return out


def merge_pages(results: list[ParseResult], dedupe_by: list[str] | None = None) -> ParseResult:
    """Merge per-page results, keeping every page's unparsed lines."""
    if not results:
        return ParseResult(rows=[], fields=[], level="card")
    out = ParseResult(
        rows=[r for res in results for r in res.rows],
        fields=results[0].fields,
        unparsed=[u for res in results for u in res.unparsed],
        total_lines=sum(res.total_lines for res in results),
        consumed_lines=sum(res.consumed_lines for res in results),
        level=results[0].level,
        notes=[n for res in results for n in res.notes],
    )
    if dedupe_by:
        out = dedupe(out, dedupe_by, out.notes)
    return out
