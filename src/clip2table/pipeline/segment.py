"""segment: split normalized lines into records.

This is where the architecture spends its budget, because a wrong boundary
does not corrupt one cell - it merges or splits records, and every later
stage then faithfully processes garbage.

Two mechanisms:
  - anchor + anchor_next: a record starts at a line matching anchor whose
    NEXT line matches anchor_next. Two-line agreement is what separates a real
    record start from a short body line; a single-line length test cannot.
  - blank-line separation: the shape most human-copied list pages arrive in.

Both are deterministic. No inference at this stage.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..models import Captured
from .normalize import collapse_ws, to_lines


@dataclass
class Segmented:
    records: list[list[str]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    total_lines: int = 0
    method: str = ""
    notes: list[str] = field(default_factory=list)
    # Lines that were accounted for without becoming a record: today only a
    # skipped table header. Without this the conservation check would report a
    # phantom lost line for every clean table with a header.
    extra_consumed: int = 0
    # Index into the line list this segmenter was handed, one per record.
    # A trace can say "record 7, line 2"; without this the reader still has to
    # count records by hand to find that line in the pasted text, which is
    # exactly the manual work the preview is supposed to remove. A short list
    # means "unknown", never zero.
    record_starts: list[int] = field(default_factory=list)
    # One list per record: the index in the source of each of its lines, in
    # order. The start alone cannot answer "which line did this value come
    # from" whenever blank lines sit inside a record, because the record drops
    # them and every later offset then points one or more lines too early.
    # Keeping the whole mapping is what makes every line's location exact
    # rather than derived. A short list means "unknown", never zero.
    record_lines: list[list[int]] = field(default_factory=list)

    @property
    def consumed_lines(self) -> int:
        return sum(len(r) for r in self.records) + self.extra_consumed


def segment(lines: list[str], *, anchor: str | None = None,
            anchor_next: str | None = None,
            blank_separated: bool = False) -> Segmented:
    """Split lines into records using an explicit card rule."""
    total = len([ln for ln in lines if ln.strip()])
    if anchor and anchor_next:
        return _segment_anchored(lines, anchor, anchor_next, total)
    if anchor:
        return _segment_anchored(lines, anchor, None, total)
    if blank_separated:
        return _segment_blank(lines, total)
    return Segmented(records=[], unparsed=[ln for ln in lines if ln.strip()],
                     total_lines=total, method="none",
                     notes=["没有提供切分规则"])


def _segment_anchored(lines: list[str], anchor: str, anchor_next: str | None,
                      total: int) -> Segmented:
    try:
        a_re = re.compile(anchor)
    except re.error as e:
        seg = Segmented(total_lines=total, method="anchor")
        seg.notes.append(f"anchor 正则非法：{e}")
        seg.unparsed = [ln for ln in lines if ln.strip()]
        return seg

    try:
        n_re = re.compile(anchor_next) if anchor_next else None
    except re.error:
        n_re = None

    starts: list[int] = []
    for i, ln in enumerate(lines):
        if not ln.strip() or not a_re.search(ln):
            continue
        if n_re is None:
            starts.append(i)
            continue
        # Two-line agreement: the immediately following non-empty line must
        # also match. This is what kills the "short body line" false positive.
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and n_re.search(lines[j]):
            starts.append(i)

    seg = Segmented(total_lines=total, method="anchor")
    if not starts:
        seg.unparsed = [ln for ln in lines if ln.strip()]
        seg.notes.append("锚点一行都没命中，全部按未识别处理")
        return seg

    # Lines before the first anchor are not part of any record: they are
    # header junk, and they go to unparsed rather than being silently dropped.
    head = [ln for ln in lines[:starts[0]] if ln.strip()]
    seg.unparsed.extend(head)

    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        chunk = [ln for ln in lines[start:end] if ln.strip()]
        if chunk:
            seg.records.append(chunk)
            seg.record_starts.append(start)
            seg.record_lines.append(
                [i for i in range(start, end) if lines[i].strip()])

    return seg


def _segment_blank(lines: list[str], total: int) -> Segmented:
    seg = Segmented(total_lines=total, method="blank")
    cur: list[str] = []
    cur_at: list[int] = []
    for i, ln in enumerate(lines):
        if ln.strip():
            cur.append(ln)
            cur_at.append(i)
        elif cur:
            seg.records.append(cur)
            seg.record_starts.append(cur_at[0])
            seg.record_lines.append(cur_at)
            cur, cur_at = [], []
    if cur:
        seg.records.append(cur)
        seg.record_starts.append(cur_at[0])
        seg.record_lines.append(cur_at)
    return seg


def classify_line(line: str) -> str:
    """Coarse shape class for one line. Says nothing about content, only form.

    Classes: date | number | kv | bullet | long | short | empty.
    """
    s = collapse_ws(line)
    if not s:
        return "empty"
    if re.match(r"^\s*\d{4}\s*[-/.年]\s*\d{1,2}", s):
        return "date"
    if _is_bare_number(s):
        return "number"
    # A bare URL is not a key:value pair. Without the "://" guard every link
    # line reads as key "https", and S2 would name a column after a URL scheme.
    if "://" not in s and re.match(r"^[^:：/]{1,18}[:：]\s*\S", s):
        return "kv"
    if re.match(r"^\s*(?:[·•*\-]|\d{1,3}[.、)）]|[（(]\d{1,3}[)）])", s):
        return "bullet"
    if len(s) > 48:
        return "long"
    return "short"


# Currency, separators and units are decoration around a number: they do not
# change the fact that the line IS a number. Keeping them out of the class
# means a price column and a title column stop looking alike, which is what
# lets a repeating 3-line product block read as a period at all.
_NUM_CHARS_RE = re.compile(r"[\s\d.,:：+\-–—%‰/×*()（）\[\]{}<>~^°#&@'\"!?]")
_CURRENCY_RE = re.compile(r"[$£€¥￥₹₩₽¢]|元|美元|欧元|日元|人民币|USD|CNY|EUR|GBP|JPY")


def _is_bare_number(s: str) -> bool:
    if not any(ch.isdigit() for ch in s):
        return False
    body = _CURRENCY_RE.sub("", s)
    if not body.strip():
        return False
    rest = _NUM_CHARS_RE.sub("", body)
    # Allow short unit tails (万/件/人/kg/cm/%...) but not words.
    if rest and not (len(rest) <= 4 and not any(ch.isalpha() for ch in rest)):
        return False
    digits = sum(ch.isdigit() for ch in body)
    return digits / max(1, len(collapse_ws(body))) >= 0.3


_KV_KEY_RE = re.compile(r"^([^:：/]{1,18})[:：]\s*\S")


def _shape_class(line: str) -> str:
    """The class used for periodicity: like classify_line, but a key:value line
    also carries its key name.

    Without the key name, a block that alternates 名称：甲 / 价格：1 has the same
    class at both offsets, so `anchor_period` - which requires the offsets to
    differ - would reject a genuinely periodic block. The key name is printed
    on screen and is just as much a shape fact as the colon itself; it is not a
    guess about the content.
    """
    base = classify_line(line)
    if base == "kv":
        m = _KV_KEY_RE.match(collapse_ws(line))
        if m:
            return "kv:" + m.group(1).strip()
    return base


# How much of each offset's group its dominant class must cover.
#
# Measured over 13 cases (7 fixtures, the books page, 5 adversarial pages), not
# chosen by taste:
#   real periods       purity 1.000   (ecom, gov, hr; books is rejected on
#                                      purity 0.513 - it is a menu, not a list)
#   two dates apart    purity 0.500-0.667 at every spacing tried (3/5/8/11)
# 0.7 sits inside that gap rather than at either edge. At 0.6 those pages pass
# and get cut into fabricated records; from 0.7 to 0.9 the answer for every
# case is the same, so the value is not a knife edge.
#
# Known ambiguity, left alone on purpose: three dates evenly spaced inside flat
# text (offsets 3/8/13) does pass at 0.7. Three consecutive periods whose k-th
# line is a date is exactly what a real five-line record looks like; no local
# evidence distinguishes them. Rejects the sparse cases, which are the ones that
# happen in practice (a stray date in a menu); does not reject this one.
_PURITY_FLOOR = 0.7

# A region may lose this share of its periods to irregularity (a wrapped body
# line, a title that carries a colon) and still be the record structure.
_MAX_MISMATCH = 0.20

# Three periods is the minimum that is not a coincidence.
_MIN_RECORDS = 3

# How many candidate region starts to try. Every class change is a candidate,
# and a page with many distinct `key:` names produces many; the cap keeps the
# scan bounded on long pages without depending on a line count.
_MAX_STARTS = 60

# Classes that can plausibly open a record (a title, a body, a stray label)
# rather than report a measurement (a price, a date).
_HEADING_CLASSES = ("short", "long")


@dataclass
class Region:
    """A run of lines that repeats with a fixed period.

    The period is a property of a **region**, not of a page. Real pages put
    navigation, filters and footer text around the list; measuring the whole
    page makes those chrome lines part of the offset arithmetic, so one title
    containing a colon can hide twenty perfectly periodic records. Identifying
    the region first is what lets the detected period describe the list and
    leave the chrome to `unparsed`.
    """

    period: int
    start: int          # first line of the region (index into filled lines)
    end: int            # one past the last line of the region
    offset: int         # where a record starts, relative to `start`
    records: int
    ratio: float
    purity: float
    dominants: tuple[str, ...]
    identical: int      # records whose whole class signature is the modal one


def anchor_region(lines: list[str]) -> Region | None:
    """The single best periodic region, or None.

    `anchor_regions` returns the ranked list; this is the convenience form for
    the callers that only act on one answer.
    """
    cands = anchor_regions(lines)
    return cands[0] if cands else None


def describe_region(r: Region) -> str:
    """The evidence behind one candidate, in words that can be checked.

    Deliberately not a score out of 100: a number invented here would look
    authoritative and mean nothing. These are counts a person can re-derive by
    looking at the text.
    """
    return (f"每 {r.period} 行一条，从第 {r.start + 1} 行起，"
            f"{r.records} 条；边界一致 {r.ratio * 100:.0f}%，"
            f"最弱一位的结构占比 {r.purity * 100:.0f}%，"
            f"完全同形的记录 {r.identical}/{r.records} 条")


def align_alternatives(lines: list[str], region: Region) -> list[int]:
    """Every offset that is a genuine rival boundary, chosen one first.

    Where a record starts is not implied by the period: two rotations of the
    same cycle repeat equally well. `_align` picks one by counting how many
    records come out identical, which settles most cases -- twenty books all
    `title/price/stock` beat the rotation that starts on a banner.

    The rotations must not be wrapped. A rotation is a claim that the first k
    lines belong to no record, so it ABANDONS them; wrapping the region's own
    lines back onto itself hides that, and makes every offset look equally good
    on any list whose records are identical -- a permanent tie, and a permanent
    "we cannot tell where records start" on text where the answer is simply
    "at the first line of the run". So a rival is only counted when it places
    the same text: at most the trailing remainder is left over, never a leading
    orphan.
    """
    filled = [ln for ln in lines if ln.strip()]
    cls = [_shape_class(ln) for ln in filled]
    a, end, p = region.start, region.end, region.period
    span = end - a
    if span < p or p < 2:
        return [region.offset]
    scored: list[tuple[tuple, int]] = []
    for off in range(p):
        nrec = (span - off) // p
        if nrec < 1:
            continue
        leftover = span - off - nrec * p
        if off + leftover >= p:
            # Starting here leaves a whole cycle's worth of lines unplaced, so
            # this is not a rival reading of the text -- it is a reading that
            # reads less of it.
            continue
        sigs = [tuple(cls[a + off + k * p:a + off + (k + 1) * p])
                for k in range(nrec)]
        ident = Counter(sigs).most_common(1)[0][1]
        head = Counter(cls[a + off:end:p]).most_common(1)[0][0]
        key = (ident, 1 if head in _HEADING_CLASSES else 0)
        scored.append((key, off))
    if not scored:
        return [region.offset]
    top = max(k for k, _ in scored)
    offsets = [off for k, off in scored if k[0] == top[0] and k[1] == top[1]]
    offsets.sort(key=lambda off: (0 if off == region.offset else 1, off))
    return offsets


def _region_rank(r: Region) -> tuple:
    """Order candidates by evidence a human would also weigh.

    Records first (a period that halves every record still repeats), then how
    many records are actually identical (structure, not just rhythm), then the
    boundary ratio, then the shorter period (Occam: do not claim a longer cycle
    when a shorter one explains the same lines).
    """
    return (r.records, r.identical, r.ratio, -r.period)


def anchor_regions(lines: list[str]) -> list[Region]:
    """Every period that survives the three tests, best first.

    More than one answer usually means the text genuinely supports more than one
    reading -- a block of five lines can be "one record of five" or "five records
    of one" depending on where the boundary falls, and both conserve the same
    lines. Picking the top silently and calling it the structure is how a tool
    produces a confident, wrong table.
    """
    filled = [ln for ln in lines if ln.strip()]
    n = len(filled)
    if n < _MIN_RECORDS * 2:
        return []
    cls = [_shape_class(ln) for ln in filled]

    starts = [0] + [i for i in range(1, n) if cls[i] != cls[i - 1]]
    if len(starts) > _MAX_STARTS:
        step = len(starts) / _MAX_STARTS
        starts = sorted({starts[int(i * step)] for i in range(_MAX_STARTS)})

    found: list[Region] = []
    for p in range(2, min(13, n // _MIN_RECORDS + 1)):
        cand = _best_region_at(cls, p, starts)
        if cand is not None:
            found.append(cand)
    # Two regions at different periods can also differ in WHERE they start; keep
    # only one per (period, start) so the list is not padded with restatements
    # of the same answer.
    seen: dict[tuple[int, int], Region] = {}
    for r in found:
        seen.setdefault((r.period, r.start), r)
    return sorted(seen.values(), key=_region_rank, reverse=True)


def _best_region_at(cls: list[str], p: int, starts: list[int]) -> Region | None:
    n = len(cls)
    bad = [0] * (n + 1)
    for i in range(n - p):
        bad[i + 1] = bad[i] + (cls[i] != cls[i + p])
    for i in range(max(0, n - p), n):
        bad[i + 1] = bad[i]

    best: Region | None = None
    for a in starts:
        if n - a < p * _MIN_RECORDS:
            continue
        cand = _scan_from(cls, p, a, bad)
        if cand is None:
            continue
        if best is None or (cand.records, cand.identical, cand.ratio) > (
                best.records, best.identical, best.ratio):
            best = cand
    return best


def _scan_from(cls: list[str], p: int, a: int, bad: list[int]) -> Region | None:
    """Widen a region from `a` in whole periods, keeping the widest qualifying."""
    n = len(cls)
    counts = [Counter() for _ in range(p)]
    tops = [("", 0)] * p
    total = [0] * p
    for j in range(a, a + p):
        off = j - a
        counts[off][cls[j]] += 1
        total[off] += 1
        tops[off] = (cls[j], counts[off][cls[j]])
    purity = [tops[off][1] / total[off] for off in range(p)]

    best: Region | None = None
    covered = a + p
    for k in range(_MIN_RECORDS, (n - a) // p + 1):
        end = a + p * k
        while covered < end:
            off = (covered - a) % p
            c = cls[covered]
            counts[off][c] += 1
            total[off] += 1
            if counts[off][c] > tops[off][1]:
                tops[off] = (c, counts[off][c])
            purity[off] = tops[off][1] / total[off]
            covered += 1

        span = end - a - p
        ratio = 1 - (bad[end - p] - bad[a]) / span
        if ratio < 1 - _MAX_MISMATCH:
            continue
        doms = {tops[off][0] for off in range(p)}
        if len(doms) < 2 or min(purity) < _PURITY_FLOOR:
            continue
        off, ident = _align(cls, a, end, p)
        cand = Region(p, a, end, off, k, ratio, min(purity),
                      tuple(sorted(doms)), ident)
        if best is None or (cand.identical, cand.records, cand.ratio) > (
                best.identical, best.records, best.ratio):
            best = cand
    return best


def _align(cls: list[str], a: int, end: int, p: int) -> tuple[int, int]:
    """(record offset, how many records share the modal signature).

    A page of twenty books is twenty identical `title/price/stock` signatures;
    a rotation that starts on the warning banner is not. When signatures tie,
    prefer a record that opens with text over one that opens with a price or a
    date - a record starts with its name, not with its measurement.
    """
    nrec = (end - a) // p
    if nrec < 1:
        return 0, 0
    sigs = [tuple(cls[a + k * p:a + (k + 1) * p]) for k in range(nrec)]
    best_key: tuple[int, int, int] | None = None
    best_off, best_ident = 0, 0
    for off in range(p):
        rot = [s[off:] + s[:off] for s in sigs]
        ident = Counter(rot).most_common(1)[0][1]
        head = Counter(cls[a + off:end:p]).most_common(1)[0][0]
        key = (ident, 1 if head in _HEADING_CLASSES else 0, -off)
        if best_key is None or key > best_key:
            best_key, best_off, best_ident = key, off, ident
    return best_off, best_ident


def anchor_period(lines: list[str]) -> tuple[int, int]:
    """(period, records) for the periodic region, or (0, 0) if there is none.

    Kept as a tuple for the callers that only need the shape of the answer;
    `anchor_region` is the one that also says *where* the region is.
    """
    region = anchor_region(lines)
    if region is None:
        return 0, 0
    return region.period, region.records


def segment_auto(lines: list[str], level: str) -> Segmented:
    """Segment without a card, guided by the sniff level."""
    total = len([ln for ln in lines if ln.strip()])
    # `record_starts` must index the list this function was handed, so the
    # positions survive the filtering the branches below do.
    where = [i for i, ln in enumerate(lines) if ln.strip()]

    if level == "tsv":
        recs = [[ln] for ln in lines if ln.strip()]
        return Segmented(records=recs, total_lines=total, method="tsv",
                         record_starts=where,
                         record_lines=[[i] for i in where])

    if level == "kv":
        return _segment_kv(lines, total)

    if level == "numbered":
        starts = [i for i, ln in enumerate(lines)
                  if re.match(r"^\s*(?:\d{1,3}[.、)）]|[（(]\d{1,3}[)）]"
                              r"|[一二三四五六七八九十]{1,2}[、.])", ln)]
        return _slice_by_starts(lines, starts, total, "numbered")

    if level == "anchored":
        return _segment_periodic([ln for ln in lines if ln.strip()], total, where)

    # lines: one record per line, single raw column. S4 is a real answer.
    return Segmented(records=[[ln] for ln in lines if ln.strip()],
                     total_lines=total, method="lines", record_starts=where,
                     record_lines=[[i] for i in where])


def _segment_kv(lines: list[str], total: int) -> Segmented:
    filled = [ln for ln in lines if ln.strip()]
    where = [i for i, ln in enumerate(lines) if ln.strip()]
    return Segmented(records=[[ln] for ln in filled], total_lines=total,
                     method="kv", record_starts=where,
                     record_lines=[[i] for i in where])


def _segment_periodic(filled: list[str], total: int,
                      where: list[int] | None = None) -> Segmented:
    """Split the periodic region into records, leaving the chrome to unparsed.

    The lines before the region (navigation, filters) and the partial record at
    either edge are not thrown away - they go to `unparsed`, so a human can see
    what the machine could not place. Discarding them would make conservation
    look perfect while data vanished.

    `where` maps an index in `filled` back to the caller's unfiltered line
    list, so `record_starts` means the same thing for every segmenter.
    """
    where = where if where is not None else list(range(len(filled)))
    region = anchor_region(filled)
    if region is None or region.period < 2:
        return Segmented(records=[[ln] for ln in filled], total_lines=total,
                         method="lines", record_starts=list(where),
                         record_lines=[[i] for i in where])

    p = region.period
    base = region.start + region.offset
    count = max(0, (region.end - base) // p)
    if count < 1:
        return Segmented(records=[[ln] for ln in filled], total_lines=total,
                         method="lines", record_starts=list(where),
                         record_lines=[[i] for i in where])

    seg = Segmented(total_lines=total, method="anchored")
    seg.unparsed.extend(filled[:base])
    for k in range(count):
        seg.records.append(filled[base + k * p:base + (k + 1) * p])
        seg.record_starts.append(where[base + k * p])
        seg.record_lines.append(where[base + k * p:base + (k + 1) * p])
    seg.unparsed.extend(filled[base + count * p:])
    if region.start:
        seg.notes.append(
            f"记录区从第 {region.start + 1} 行开始，前面 {region.start} 行"
            f"（导航/筛选等）没有被切进任何记录，已放进未识别")
    return seg


def _slice_by_starts(lines: list[str], starts: list[int], total: int,
                     method: str) -> Segmented:
    seg = Segmented(total_lines=total, method=method)
    if not starts:
        seg.records = [[ln] for ln in lines if ln.strip()]
        seg.method = "lines"
        at = [i for i, ln in enumerate(lines) if ln.strip()]
        seg.record_starts = at
        seg.record_lines = [[i] for i in at]
        seg.notes.append("有分级信号但找不到起点，退化为一行一记录")
        return seg
    if starts and starts[0] > 0:
        seg.unparsed.extend([ln for ln in lines[:starts[0]] if ln.strip()])
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        chunk = [ln for ln in lines[s:e] if ln.strip()]
        if chunk:
            seg.records.append(chunk)
            seg.record_starts.append(s)
            seg.record_lines.append(
                [i for i in range(s, e) if lines[i].strip()])
    return seg
