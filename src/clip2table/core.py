"""core: one capture in, one parse result out. The seam every command uses.

Card present  -> card rules decide the boundary and the fields.
Card absent   -> infer's ladder decides, and returns the card draft to match.
"""
from __future__ import annotations

from .cards.schema import Card
from .models import Captured, ParseResult
from .pipeline.assemble import AssembleOptions, assemble
from .pipeline.extract import FieldSpec
from .pipeline.infer import InferResult, infer
from .pipeline.normalize import normalize_text, to_lines
from .pipeline.segment import segment
from .pipeline.sniff import sniff


def parse_capture(cap: Captured, card: Card | None = None) -> tuple[ParseResult, InferResult | None]:
    """Returns (result, infer_result_or_None)."""
    if card is None:
        inf = infer(cap)
        return inf.result, inf

    lines = to_lines(normalize_text(_text_of(cap)))
    seg = segment(
        lines,
        anchor=card.anchor,
        anchor_next=card.anchor_next,
        blank_separated=(card.segment_mode == "blank"),
    )
    # The two rebuilt modes below replace `seg.records` wholesale, so the starts
    # `segment()` filled no longer match them. They are filled again from here.
    _where = [i for i, ln in enumerate(lines) if ln.strip()]
    if card.segment_mode == "periodic":
        from .marks import reslice
        if len(_where) <= card.segment_offset:
            # An empty/short later page leaves all source lines available for review.
            seg.records, seg.record_starts, seg.record_lines = [], [], []
            seg.unparsed = [lines[i] for i in _where]
        else:
            seg = reslice(lines, card.segment_period, card.segment_offset, card.segment_count)
    elif card.segment_mode == "single":
        filled = [ln for ln in lines if ln.strip()]
        seg.records = [filled] if filled else []
        seg.total_lines = len(filled)
        seg.record_starts = _where[:1]
        seg.record_lines = [list(_where)] if filled else []
        seg.unparsed = []
        seg.method = "single"
    elif card.segment_mode == "lines":
        # One line, one record. This is what a table card needs: the rows were
        # already separated by the source (a TSV paste, an HTML <tr>), so
        # grouping them further is not a rule, it is a bug. `single` collapsed
        # a whole pasted table into one record whose first cell was the header
        # row -- 11 lines became 1 row with 100% fill on the first two fields,
        # which reads like a clean parse.
        record_at = list(_where)
        if cap.kind == "html" and any(f.kind == "cell" for f in card.fields):
            from .pipeline.htmltable import html_line_blocks
            blocks = html_line_blocks(cap.payload or "")
            if any(kind == "row" for kind, _ in blocks):
                record_at = []
                pos = 0
                for kind, text in blocks:
                    block_lines = to_lines(normalize_text(text))
                    if kind == "row":
                        record_at.extend(pos + i for i, ln in enumerate(block_lines) if ln.strip())
                    pos += len(block_lines)
        record_set = set(record_at)
        others = [lines[i] for i in _where if i not in record_set]
        filled = [lines[i] for i in record_at]
        head = []
        if card.skip_header and filled:
            head = [filled[0]]
            filled = filled[1:]
        seg.records = [[ln] for ln in filled]
        seg.total_lines = len(_where)
        # `segment()` above never ran for this mode, so the starts it would have
        # filled are filled here. Without them the preview says a value came
        # from "记录内第 1 行" for every row, which reads as if the whole table
        # were one record.
        seg.record_starts = record_at[len(head):]
        # One single-index list per one-line record. `lines` mode is the one
        # that cannot drift (each record is one line), but filling it keeps
        # `raw_at` meaning the same thing in every mode.
        seg.record_lines = [[i] for i in record_at[len(head):]]
        # The header row is consumed (it is the column names), not unparsed:
        # putting it in unparsed would report a phantom "未识别 1 行" for every
        # clean table.
        seg.extra_consumed = len(head)
        seg.unparsed = others
        seg.method = "lines"
        seg.notes.append(
            "首行当表头，未计入记录" if head else "一行一记录（没有跳过表头）")

    if card.min_lines > 1:
        keep: list[list[str]] = []
        kept_at: list[int] = []
        kept_lines: list[list[int]] = []
        for i, rec in enumerate(seg.records):
            if len(rec) >= card.min_lines:
                keep.append(rec)
                if i < len(seg.record_starts):
                    kept_at.append(seg.record_starts[i])
                if i < len(seg.record_lines):
                    kept_lines.append(seg.record_lines[i])
            else:
                seg.unparsed.extend(rec)
        seg.records = keep
        if len(kept_at) == len(keep):
            seg.record_starts = kept_at
        else:
            # The dropped records broke the one-to-one match; a start list that
            # no longer lines up would point at the wrong rows.
            seg.record_starts = []
        if len(kept_lines) == len(keep):
            seg.record_lines = kept_lines
        else:
            seg.record_lines = []

    res = assemble(
        seg, card.fields,
        AssembleOptions(dedupe_by=card.dedupe_key or None),
    )
    _check_structure(card, seg, res)
    return res, None


def _check_structure(card: Card, seg, res: ParseResult) -> None:
    """Compare this page against what the card says records look like.

    The card already fixes WHERE a record starts. That says nothing about
    whether the records that came out are the shape the card was written for:
    a site can keep its anchors while dropping a line from each record, and the
    result is a table whose columns are all filled from the wrong lines. The
    card's declared expectations are the only cheap way to notice, so they are
    checked here, on every page, and reported per page.
    """
    if not card.record_lines and not card.required:
        return
    lo, hi = card.record_lines or (0, 10 ** 9)
    short = [i for i, rec in enumerate(seg.records, 1) if len(rec) < lo]
    long = [i for i, rec in enumerate(seg.records, 1) if len(rec) > hi]
    if short:
        res.deviations.append(
            f"{len(short)}/{len(seg.records)} 条记录不足 {lo} 行"
            f"（第 {_few(short)} 条）")
        res.deviation_rows.extend(short)
    if long:
        res.deviations.append(
            f"{len(long)}/{len(seg.records)} 条记录超过 {hi} 行"
            f"（第 {_few(long)} 条）")
        res.deviation_rows.extend(long)
    for name in card.required:
        missing = [i for i, row in enumerate(res.rows, 1)
                   if not _filled(row.values.get(name))]
        if missing:
            res.deviations.append(
                f"必填字段 {name} 有 {len(missing)}/{len(res.rows)} 条为空"
                f"（第 {_few(missing)} 条）")
            res.deviation_rows.extend(missing)


def _few(nums: list[int], limit: int = 5) -> str:
    head = "、".join(str(n) for n in nums[:limit])
    return head + ("…" if len(nums) > limit else "")


def _filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def _text_of(cap: Captured) -> str:
    if cap.kind == "html":
        from .pipeline.htmltable import html_to_lines
        return "\n".join(html_to_lines(cap.payload or ""))
    return cap.payload or ""


def specs_of(card: Card | None, inf: InferResult | None) -> list[FieldSpec]:
    if card is not None:
        return card.fields
    return inf.specs if inf else []
