"""sniff: decide what kind of structure arrived, before any parsing.

The order matters. HTML tables are the most informative, so they win. Then
TSV (Excel paste). Then periodic anchors. Then k:v. Then numbered lines.
Then, honestly, "this is just lines" - which is a valid answer, not a failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Captured


@dataclass
class Sniff:
    kind: str                       # html_table | html_list | tsv | anchored | kv | numbered | lines
    confidence: float               # 0..1
    reasons: list[str] = field(default_factory=list)
    n_cols: int = 0


_TAG_RE = re.compile(r"<\s*(table|tr|td|th)\b", re.I)


def sniff(cap: Captured) -> Sniff:
    if cap.kind == "json":
        return Sniff("json", 1.0, ["输入本身是 JSON"])
    if cap.kind == "html":
        return _sniff_html(cap.payload or "")
    return _sniff_text(cap.payload or "")


def _sniff_html(html: str) -> Sniff:
    tags = {m.group(1).lower() for m in _TAG_RE.finditer(html)}
    if "table" in tags and ("tr" in tags or "td" in tags or "th" in tags):
        rows = len(re.findall(r"<\s*tr\b", html, re.I))
        return Sniff("html_table", 0.95, [f"HTML 里有 table/tr（{rows} 个 tr 标签）"])
    blocks = len(re.findall(r"<\s*(li|p|div|article)\b", html, re.I))
    if blocks >= 3:
        return Sniff("html_list", 0.6, [f"HTML 里没有表格，但有 {blocks} 个块级元素"])
    return Sniff("html_list", 0.3, ["HTML 里结构信息很弱，只能按块切"])


def _sniff_text(text: str) -> Sniff:
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return Sniff("lines", 0.0, ["没有非空行"])

    # TSV: consistent tab counts across many lines is Excel's signature.
    tab_counts = [ln.count("\t") for ln in nonempty]
    pos = [c for c in tab_counts if c > 0]
    if len(pos) >= 3:
        from collections import Counter
        common, freq = Counter(pos).most_common(1)[0]
        if freq / len(nonempty) >= 0.6:
            return Sniff("tsv", 0.9,
                         [f"{freq}/{len(nonempty)} 行都有 {common} 个制表符（列数一致）"],
                         n_cols=common + 1)

    # k:v: same separator appearing at line starts repeatedly.
    for sep in ("：", ":"):
        kv = sum(1 for ln in nonempty if re.match(rf"^[^:：]{{1,12}}{sep}", ln))
        if kv >= 3 and kv / len(nonempty) >= 0.5:
            return Sniff("kv", 0.7, [f"{kv}/{len(nonempty)} 行是「键{sep}值」形状"])

    # numbered: 1. / 1、/ (1) / 一、
    num = sum(1 for ln in nonempty
              if re.match(r"^\s*(?:\d{1,3}[.、)）]|[（(]\d{1,3}[)）]|[一二三四五六七八九十]{1,2}[、.])", ln))
    if num >= 3 and num / len(nonempty) >= 0.4:
        return Sniff("numbered", 0.6, [f"{num}/{len(nonempty)} 行以序号开头"])

    # anchored: a short line recurring every few lines suggests record starts.
    from .segment import anchor_period
    period, hits = anchor_period(nonempty)
    if period >= 2 and hits >= 3:
        return Sniff("anchored", 0.5, [f"疑似周期锚点：每 {period} 行出现一次短行（{hits} 次）"])

    return Sniff("lines", 0.3, ["没找到任何周期结构，按一行一记录处理"])
