"""Convert source-line marks into record anchors and field positions.

Line numbers refer to the normalized source, including blank lines. Record
periods and candidate offsets count non-empty lines, matching the segmenter.
"""
from __future__ import annotations

import re

from .errors import InputError

# Characters that stand for "something that varies between records". Digits and
# Latin runs are what change row to row (order numbers, prices, dates); CJK text
# is what usually identifies the *shape* of a line and is kept literally.
_DIGITS = re.compile(r"\d+")


def line_no(text: str, total: int) -> int:
    """A 1-based line number typed by a person -> a 0-based index.

    Returns -1 for "no answer", which every caller treats as "use inference".
    Out-of-range input is an error: silently clamping would move the boundary
    to a line the user did not pick.
    """
    s = (text or "").strip()
    if not s:
        return -1
    if not s.isdigit():
        raise InputError(f"「{s}」不是行号", hint="填原文左边那个数字，比如 4")
    n = int(s)
    if not (1 <= n <= total):
        raise InputError(f"行号 {n} 超出范围（这份样本共 {total} 行）",
                         hint=f"填 1 到 {total} 之间的数字")
    return n - 1


def period_of(start: int, end: int) -> int:
    """How many lines one record occupies, from the two marks."""
    if end < start:
        raise InputError("结束行在开始行之前", hint="先标记一条记录的第一行，再标记它的最后一行")
    return end - start + 1


def _generalize(text: str) -> str:
    """A pattern that keeps the shape of a line and lets the varying parts move."""
    parts: list[str] = []
    end = 0
    text = text.strip()
    for match in re.finditer(r"\d+|[A-Za-z]+", text):
        parts.append(re.escape(text[end:match.start()]))
        parts.append(r"\d+" if match.group().isdigit() else "[A-Za-z]+")
        end = match.end()
    parts.append(re.escape(text[end:]))
    return r"^\s*" + "".join(parts) + r"\s*$"


def anchor_from(lines: list[str], start: int, period: int) -> tuple[str, list[int]]:
    """Match all expected starts after the marked line, using non-empty periods."""
    if not (0 <= start < len(lines)):
        raise InputError("标记的起始行不在这份样本里")
    text = lines[start].strip()
    if not text:
        raise InputError("标记的起始行是空行",
                         hint="一条记录不会从一个空行开始，往上或往下挑一行")

    if period <= 0:
        raise InputError("记录长度至少是 1 行")
    where = [i for i, ln in enumerate(lines) if ln.strip()]
    offset = where.index(start)
    expect = where[offset::period]

    # Widening moves from "the whole line" to "its leading words": a prefix is
    # looser, so it is tried only after the strict form failed to land.
    attempts: list[str] = [_generalize(text)]
    words = text.split()
    if len(words) > 1:
        attempts.append(_generalize(" ".join(words[:2])))
    attempts.append("^" + _DIGITS.sub(r"\\d+", re.escape(words[0] if words else text[:12])))

    best: tuple[str, list[int]] | None = None
    for pat in attempts:
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        hits = [i for i, ln in enumerate(lines) if ln.strip() and rx.search(ln)]
        if not hits:
            continue
        if hits == expect:
            return pat, hits
        # Keep the least-bad option so the error message can say what it found.
        if best is None or len(hits) < len(best[1]):
            best = (pat, hits)

    # Nothing landed. There are two different reasons, and they need different
    # advice: a rule that matches too much is a bad marker, while a rule that
    # matches only the one line the user picked is not a rule at all -- that
    # sample just has no repeating prefix, and no pattern will invent one.
    if best is not None:
        pat, hits = best
        missing = [i + 1 for i in expect if i not in hits]
        stray = [i + 1 for i in hits if i not in expect][:5]
        if stray:
            raise InputError(
                f"从第 {start + 1} 行推出的规则还会匹配 {len(stray)} 行"
                f"不该匹配的行（第 {stray} 行），不能用来切记录",
                hint="换一条只在记录开头出现的行做标记，比如带订单号的那一行")
        if missing:
            raise InputError(
                f"从第 {start + 1} 行推出的规则漏掉了记录起点"
                f"（第 {missing[:5]} 行）",
                hint="检查标记的起止行是否覆盖一条完整记录，或换一个重复出现的起始行")
    raise InputError(
        f"第 {start + 1} 行看起来不像每条记录都有的一行",
        hint="这一页可能没有固定的开头。可以改成按条数切："
             "task fix 之前先手工把 mode 写成 \"lines\" 或 \"blank\"，"
             "或者选一条每条记录都有的行（订单号、日期那行）做标记")


def find_in_record(record_lines: list[str], value: str) -> int:
    """Which line of the marked record holds `value`, for `task fix --value`."""
    hit = [i for i, ln in enumerate(record_lines) if value and value in ln]
    if not hit:
        raise InputError(
            f"标记的记录里找不到「{value}」",
            hint="值要能在你自己标的这条记录里看到，可能被截断或换了行")
    if len(hit) > 1:
        raise InputError(
            f"「{value}」在这一条记录里出现了 {len(hit)} 次（第 "
            f"{[i + 1 for i in hit]} 行）",
            hint="换一个只出现一次的值，或者直接用 --from-line 指行号")
    return hit[0]


def reslice(lines: list[str], period: int, offset: int, count: int | None = None):
    """Re-cut the source with a period and an offset that a person chose.

    The inference ladder reports alternates with exactly these two numbers, so
    "切法 2" has to be applicable without inventing a regex to express it. Each
    record is a slice of `period` consecutive lines starting at `offset`, then
    every `period` lines after; everything before the first record is left for
    `unparsed` rather than dropped.
    """
    from .pipeline.segment import Segmented

    if period < 1:
        raise InputError(f"每条记录的行数至少是 1，实际是 {period}")
    if offset < 0:
        raise InputError(f"起点不能是负数，实际是 {offset}")

    where = [i for i, ln in enumerate(lines) if ln.strip()]
    filled = [lines[i] for i in where]
    if not filled:
        raise InputError("这份样本里没有非空行，没法切")
    if offset >= len(filled):
        raise InputError(
            f"起点是第 {offset + 1} 行，但这份样本只有 {len(filled)} 行非空内容")

    seg = Segmented(total_lines=len(filled), method="picked")
    seg.unparsed.extend(filled[:offset])
    stop = min(len(filled), offset + count * period) if count is not None else len(filled)
    for s in range(offset, stop, period):
        seg.records.append(filled[s:s + period])
        # `where` maps a filtered index back to the pasted text, so a preview
        # keeps meaning "line N of the original" in this mode too.
        seg.record_starts.append(where[s])
        seg.record_lines.append(where[s:s + period])
    seg.unparsed.extend(filled[stop:])
    if offset:
        seg.notes.append(
            f"从第 {where[offset] + 1} 行起算，前面 {offset} 行没有切进任何记录")
    return seg
