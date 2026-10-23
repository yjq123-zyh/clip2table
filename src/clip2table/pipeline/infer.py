"""infer: get a usable table out of the first capture, with no card at all.

The ladder is ordered by how much structure the source actually supports:

  S0  table structure exists (HTML <table>, or TSV with consistent columns)
  S1  periodic short-line anchors (segmentation by repetition)
  S2  key:value pairs
  S3  numbered / bulleted lines
  S4  one line = one record, single raw column

S4 is not a failure. It is the honest answer "there is no record structure
here, so here are the lines" - which is worth more than an invented structure.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..models import Captured, ParseResult, Row
from .extract import FieldSpec
from .htmltable import html_to_lines
from .normalize import collapse_ws, normalize_text, to_lines
from .segment import (align_alternatives, anchor_period, anchor_regions,
                      describe_region, segment_auto)
from .sniff import Sniff, sniff

_KV_RE = re.compile(r"^\s*([^:：]{1,18})[:：]\s*(.*)$")
_NUM_RE = re.compile(r"^\s*(?:\d{1,3}[.、)）]|[（(]\d{1,3}[)）]"
                     r"|[一二三四五六七八九十]{1,2}[、.])")


@dataclass
class Alternate:
    """One other reading of the same text that also survives every test."""

    level: str
    why: str
    period: int
    field_count: int
    record_count: int
    consumed: int
    unparsed: int
    sample: list[list[str]] = field(default_factory=list)

    def line(self) -> str:
        return (f"{self.level}：每 {self.period} 行一条 → {self.record_count} 条 × "
                f"{self.field_count} 列（{self.why}）")


@dataclass
class InferResult:
    result: ParseResult
    level: str
    sniff: Sniff
    specs: list[FieldSpec] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Other segmentations the text supports about as well as the chosen one.
    # Empty means the evidence pointed one way, NOT that the answer was checked
    # against human intent -- those are different claims.
    alternates: list[Alternate] = field(default_factory=list)
    # Why the chosen reading won, in countable terms (repeat counts, boundary
    # agreement, how many records are actually identical). Never a fabricated
    # percentage: a number with no calibration behind it reads as authority it
    # has not earned.
    evidence: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return bool(self.alternates)

    def headline(self) -> str:
        """The one line a caller must be able to read and repeat."""
        res = self.result
        rates = res.fill_rates
        avg = sum(rates.values()) / len(rates) if rates else 0.0
        tail = f" ｜ 另有 {len(self.alternates)} 种切法" if self.alternates else ""
        return (f"{self.level} ｜ 字段 {len(res.fields)} ｜ "
                f"覆盖 {avg * 100:.0f}% ｜ 未识别 {len(res.unparsed)} 行 ｜ "
                f"{len(res.rows)} 行{tail}")


def infer(cap: Captured) -> InferResult:
    sn = sniff(cap)
    lines = to_lines(normalize_text(_capture_to_text(cap)))

    if sn.kind in ("html_table", "tsv"):
        out = _s0(sn, lines)
        out.evidence = [f"HTML/TSV 里有真表格结构：{out.result.fields and len(out.result.fields)} 列"]
        return out
    # A credible period outranks kv and numbered: a period keeps every line
    # inside some record, while kv drops the lines that are not key:value and
    # numbered rewrites them. Conservation wins before convenience.
    filled = [ln for ln in lines if ln.strip()]
    regions = anchor_regions(filled)
    if regions and regions[0].period >= 2 and regions[0].records >= 3:
        out = _s1(sn, lines, regions[0].period)
        out.evidence = [f"周期候选 1：{describe_region(r)}" for r in regions[:3]]
        out.alternates = _alternates_from_regions(regions[1:], regions[0], lines)
        return out
    if sn.kind == "kv":
        return _s2(sn, lines)
    if sn.kind == "numbered":
        return _s3(sn, lines)
    return _s4(sn, lines)


def _alternates_from_regions(others, winner, lines: list[str]) -> list["Alternate"]:
    """The other readings the same text supports.

    Two kinds, and both are real:

    * a different PERIOD that produces a comparable number of records. A period
      yielding far fewer records is not a rival reading, it is a worse one (p=2
      on a three-line block leaves a third of the text unplaced, and that gap is
      the decision), so only candidates within a fifth of the winner count.
    * a different OFFSET at the SAME period that ties on every measurable
      criterion. Where a record starts is not implied by its length; when two
      rotations produce the same number of identical records, the text has not
      said which one is the record.

    Neither is reported as a "correct" version. The point is that a person sees
    that more than one answer fits, instead of receiving the top-scorer with no
    indication that a decision was made.
    """
    filled = [ln for ln in lines if ln.strip()]
    out: list["Alternate"] = []
    for r in others:
        if r.records < winner.records * 0.8:
            continue
        out.append(_alternate_of(r, filled,
                                 f"边界一致 {r.ratio * 100:.0f}%、"
                                 f"同形 {r.identical}/{r.records} 条",
                                 "另一个周期"))
    # Rotations tie on every structural measure, so listing all of them is not
    # extra information -- it is the same sentence repeated. One is enough to
    # tell the reader the boundary was not determined by the text; which one is
    # shown does not matter, because none of them is more supported than the
    # others.
    ties = [off for off in align_alternatives(filled, winner)
            if off != winner.offset]
    if ties:
        out.append(_alternate_of(
            winner, filled,
            f"每 {winner.period} 行一条的节奏成立，但起点每错开一行都能得到同形记录"
            f"（{winner.identical}/{winner.records} 条一样），文本没有指定从哪一行起算",
            "起点不唯一", offset=ties[0]))
    return out


def _alternate_of(r, filled: list[str], why: str, kind: str,
                  offset: int | None = None) -> "Alternate":
    off = r.offset if offset is None else offset
    base = r.start + off
    recs: list[list[str]] = []
    for k in range(r.records):
        chunk = filled[base + k * r.period:base + (k + 1) * r.period]
        if chunk:
            recs.append(chunk)
    consumed = len(filled) - max(0, base) - max(0, len(filled) - (
        base + r.records * r.period))
    return Alternate(level="S1", why=f"{why}（{kind}）", period=r.period,
                     field_count=r.period, record_count=len(recs),
                     consumed=max(0, consumed),
                     unparsed=max(0, len(filled) - consumed), sample=recs)


def _capture_to_text(cap: Captured) -> str:
    if cap.kind == "html":
        return "\n".join(html_to_lines(cap.payload or ""))
    return cap.payload or ""


def _wrap(rows: list[Row], fields: list[str], level: str, sn: Sniff,
          unparsed: list[str], total: int, notes: list[str],
          consumed: int | None = None) -> InferResult:
    """Build the result. `consumed` must be counted from the records, not
    derived from the total - deriving it makes the conservation check
    unfailable, which is the same as not having one.
    """
    if consumed is None:
        consumed = sum(len(r.raw_lines) for r in rows)
    res = ParseResult(rows=rows, fields=fields, unparsed=unparsed,
                      total_lines=total, consumed_lines=consumed,
                      level=level, notes=notes)
    sample = [{f: r.values.get(f) for f in fields} for r in rows[:3]]
    return InferResult(result=res, level=level, sniff=sn, sample=sample, notes=notes)


# ------------------------------------------------------------------ S0

def _s0(sn: Sniff, lines: list[str]) -> InferResult:
    """Table structure exists - but only for the lines that ARE the table.

    A page can contain one 4-column table and 40 lines of navigation. Treating
    every line as a record because *a* table exists somewhere invents records
    out of the site's chrome, and picking the column count as the most common
    tab count over the whole page yields 1 (most lines have no tabs at all),
    which then names the first nav link as the header. That happened on a real
    page: 12 real rows disappeared into "42 rows x 1 column, field: Domains".

    So the column count comes from the lines that actually carry tabs, and only
    lines with that exact count become records. Everything else is preserved in
    unparsed instead of being reshaped into a row it never was.
    """
    filled = [ln for ln in lines if ln.strip()]
    total = len(filled)
    if not filled:
        return _s4(sn, lines)

    tabbed = [ln for ln in filled if ln.count("\t") > 0]
    if not tabbed:
        # "table exists in the HTML" but not one row survived as tab-separated
        # text. Do not invent columns from nothing.
        return _s4(sn, lines)

    ncols = sn.n_cols or Counter(ln.count("\t") for ln in tabbed).most_common(1)[0][0] + 1

    same = [ln for ln in filled if ln.count("\t") + 1 == ncols]
    others = [ln for ln in filled if ln not in same]
    if not same:
        return _s4(sn, lines)

    head = [collapse_ws(c) for c in same[0].split("\t")]
    has_header = (len(head) == ncols and all(head)
                  and not any(re.fullmatch(r"[-+]?[\d.,]+", c) for c in head))

    fields = head if has_header else [f"col{i + 1}" for i in range(ncols)]
    data = same[1:] if has_header else same

    notes = [f"S0：检测到 {ncols} 列表格结构（{sn.reasons[0] if sn.reasons else ''}）"]
    if not has_header:
        notes.append("首行不像表头（空列或全数字），列名用 col1..colN")
    if others:
        notes.append(f"另有 {len(others)} 行不是 {ncols} 列，未计入记录，"
                     f"已保留在未识别里")

    rows: list[Row] = []
    for line in data:
        cells = line.split("\t")
        vals = {f: (cells[i].strip() or None) if i < len(cells) else None
                for i, f in enumerate(fields)}
        if any(v for v in vals.values()):
            rows.append(Row(values=vals, raw_lines=[line]))

    specs = [FieldSpec(name=f, kind="cell", col=i) for i, f in enumerate(fields)]
    consumed = len(rows) + (1 if has_header else 0)
    out = _wrap(rows, fields, "S0", sn, others, total, notes, consumed=consumed)
    out.specs = specs
    return out


# ------------------------------------------------------------------ S1

def _s1(sn: Sniff, lines: list[str], period: int) -> InferResult:
    filled = [ln for ln in lines if ln.strip()]
    total = len(filled)
    seg = segment_auto(filled, "anchored")
    if not seg.records:
        return _s4(sn, lines)

    width = max(len(r) for r in seg.records)
    fields, strippers = _column_names(seg.records, width)
    rows = []
    for rec in seg.records:
        vals = {}
        for i, f in enumerate(fields):
            cell = rec[i] if i < len(rec) else None
            vals[f] = strippers[i](cell) if (cell is not None and strippers[i]) else cell
        rows.append(Row(values=vals, raw_lines=list(rec)))
    notes = [f"S1：按周期锚点切成 {len(rows)} 条记录（每 {period} 行一条）",
             "列名按每列的形态推断，键值列已去掉「键：」前缀，可在卡里改成业务名"]
    out = _wrap(rows, fields, "S1", sn, list(seg.unparsed), total, notes)
    out.specs = _s1_specs(fields, seg.records)
    return out


def _s1_specs(fields: list[str], records: list[list[str]]) -> list[FieldSpec]:
    """Specs that reproduce exactly what _s1's rows already contain.

    A key:value column is stripped in the rows, so its spec must be a search
    that captures the part after the colon. Emitting a plain line slice here
    would make the draft card produce `规格：黑色` where infer's own table says
    `黑色` - the tool disagreeing with itself.
    """
    specs: list[FieldSpec] = []
    for i, f in enumerate(fields):
        cells = [r[i] for r in records if i < len(r) and r[i].strip()]
        if cells and all(re.match(rf"^\s*{re.escape(f)}\s*[:：]", c) for c in cells):
            specs.append(FieldSpec(
                name=f, kind="search",
                pattern=rf"^\s*{re.escape(f)}\s*[:：]\s*(?P<v>.*)$"))
        else:
            specs.append(FieldSpec(name=f, kind="line", index=i))
    return specs


def _column_names(records: list[list[str]],
                  width: int) -> tuple[list[str], list[Any]]:
    """Name each column by what its cells look like, and say how to clean it.

    A date column is a date column no matter which offset it sits at. A
    key:value column already carries its own name in every cell — so use that
    name for the header and strip the prefix from the values, otherwise the
    header says 规格 and the cell still says 规格：黑色. The lineN fallback is
    honest: that column has no signature worth naming.
    """
    names: list[str] = []
    strippers: list[Any] = []
    for i in range(width):
        cells = [r[i] for r in records if i < len(r)]
        name, strip = _describe_column(cells, i + 1)
        names.append(name)
        strippers.append(strip)

    seen: dict[str, int] = {}
    for i, n in enumerate(names):
        if names.count(n) > 1:
            seen[n] = seen.get(n, 0) + 1
            names[i] = f"{n}{seen[n]}"
    return names, strippers


def _describe_column(cells: list[str], ordinal: int) -> tuple[str, Any]:
    if not cells:
        return f"line{ordinal}", None
    kind, share = Counter(_cell_kind(c) for c in cells).most_common(1)[0]
    if share / len(cells) < 0.6:
        return f"line{ordinal}", None
    if kind == "date":
        return "日期", None
    if kind == "kv":
        keys = [m.group(1).strip() for c in cells if (m := _KV_RE.match(c))]
        if keys:
            key = Counter(keys).most_common(1)[0][0]
            prefix = re.compile(rf"^\s*{re.escape(key)}\s*[:：]\s*")

            def strip(v: str, _p=prefix) -> str:
                return _p.sub("", v, count=1)

            return key, strip
    if kind == "bullet":
        return "序号", None
    return f"line{ordinal}", None


def _cell_kind(text: str) -> str:
    from .segment import classify_line
    return classify_line(text)


# ------------------------------------------------------------------ S2

def _s2(sn: Sniff, lines: list[str]) -> InferResult:
    filled = [ln for ln in lines if ln.strip()]
    total = len(filled)
    pairs: list[tuple[str, str]] = []
    # Lines that are not key:value are not silently discarded: they go to
    # unparsed, because the only thing worse than a missing field is data
    # that disappeared from the report entirely.
    leftover: list[str] = []
    for ln in filled:
        m = _KV_RE.match(ln)
        if m:
            pairs.append((collapse_ws(m.group(1)), m.group(2).strip()))
        else:
            leftover.append(ln)
    if not pairs:
        return _s4(sn, lines)

    repeated = [k for k, n in Counter(k for k, _ in pairs).items() if n >= 2]
    if repeated:
        boundary = repeated[0]
        blocks: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        for k, v in pairs:
            if k == boundary and cur:
                blocks.append(cur)
                cur = []
            cur.append((k, v))
        if cur:
            blocks.append(cur)

        fields = list(dict.fromkeys(k for b in blocks for k, _ in b))
        rows = [Row(values={f: (dict(b).get(f) or None) for f in fields},
                    raw_lines=[f"{k}{'：'}{v}" for k, v in b]) for b in blocks]
        notes = [f"S2：键值对，按重复出现的「{boundary}」切成 {len(rows)} 条",
                 "每行一个字段，源里没有的字段留空"]
        out = _wrap(rows, fields, "S2", sn, leftover, total, notes)
        out.specs = [FieldSpec(name=f, kind="search",
                               pattern=rf"^\s*{re.escape(f)}[:：]\s*(?P<v>.*)$")
                     for f in fields]
        return out

    rows = [Row(values={"key": k, "value": v}, raw_lines=[f"{k}：{v}"])
            for k, v in pairs]
    notes = ["S2：键值对，没有重复键，按两列输出（key/value）"]
    out = _wrap(rows, ["key", "value"], "S2", sn, leftover, total, notes)
    out.specs = [FieldSpec(name="key", kind="line", index=0),
                 FieldSpec(name="value", kind="line", index=0)]
    return out


# ------------------------------------------------------------------ S3

def _s3(sn: Sniff, lines: list[str]) -> InferResult:
    filled = [ln for ln in lines if ln.strip()]
    total = len(filled)
    numbered = [ln for ln in filled if _NUM_RE.match(ln)]
    leftover = [ln for ln in filled if not _NUM_RE.match(ln)]

    # Only the number prefix is stripped; the rest of the line is kept as-is.
    body = [(_NUM_RE.sub("", ln, count=1) if _NUM_RE.match(ln) else ln).strip()
            for ln in numbered]

    for sep in ("\t", "|", " - ", "——"):
        if not body:
            break
        hits = sum(1 for ln in body if sep in ln)
        if hits >= max(3, int(len(body) * 0.6)):
            parts = [ln.split(sep) for ln in body]
            ncols = max(len(p) for p in parts)
            fields = [f"col{i + 1}" for i in range(ncols)]
            rows = [Row(values={f: (p[i].strip() or None) if i < len(p) else None
                                for i, f in enumerate(fields)}, raw_lines=[ln])
                    for p, ln in zip(parts, numbered)]
            notes = [f"S3：去掉序号后按「{sep}」切出 {ncols} 列，共 {len(rows)} 行"]
            out = _wrap(rows, fields, "S3", sn, leftover, total, notes)
            out.specs = [FieldSpec(name=f, kind="cell", col=i)
                         for i, f in enumerate(fields)]
            return out

    rows = [Row(values={"index": i + 1, "text": b}, raw_lines=[ln])
            for i, (b, ln) in enumerate(zip(body, numbered))]
    notes = ["S3：有序号但没有稳定分隔符，输出 index + text 两列"]
    out = _wrap(rows, ["index", "text"], "S3", sn, leftover, total, notes)
    out.specs = [FieldSpec(name="index", kind="line", index=0),
                 FieldSpec(name="text", kind="line", index=0)]
    return out


# ------------------------------------------------------------------ S4

def _s4(sn: Sniff, lines: list[str]) -> InferResult:
    filled = [ln for ln in lines if ln.strip()]
    total = len(filled)
    rows = [Row(values={"raw": ln}, raw_lines=[ln]) for ln in filled]
    notes = ["S4：没找到记录边界，按一行一记录输出单列 raw",
             "这不是失败：源文本里确实没有可切分的重复结构",
             "要让多行变成一条记录，用 -p 指定一张卡的 [segment]"]
    out = _wrap(rows, ["raw"], "S4", sn, [], total, notes)
    out.specs = [FieldSpec(name="raw", kind="line", index=0)]
    return out


def describe_structure(sn: Sniff) -> str:
    """One sentence explaining why the ladder chose what it chose."""
    if sn.kind == "anchored":
        return "有周期结构，但周期未被确认，按周期切"
    return ""
