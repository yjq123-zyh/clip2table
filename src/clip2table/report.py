"""report: what the tool says about its own work.

Three things, always, in this order:
  1. the headline - channel + engine + level + rows (what a caller repeats)
  2. the fill rates - which field is thin, so a broken rule is visible
  3. the conservation check - did any line vanish

A run that produced a table and lost lines must not look like a clean run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Captured, ParseResult, _nonempty, fmt_pct

_STATUS_CN = {
    "missing": "这条记录里没有",
    "out-of-range": "规则指到记录外面",
    "no-match": "正则没匹配上",
    "coerce-failed": "类型转换失败",
    "unimplemented": "该原语未实现",
    "rule-error": "规则本身报错",
    "no-trace": "没有留痕（旧数据）",
}


def _filled(v: Any) -> bool:
    return _nonempty(v)


# Keys that carry live objects for rendering and must not reach JSON.
_INTERNAL = {"field_specs"}


def _specs(items: list[Any]) -> list[tuple[str, str, str]]:
    """Accept FieldSpec objects or plain (name, type, rule) triples."""
    out: list[tuple[str, str, str]] = []
    for it in items:
        if isinstance(it, tuple):
            name, typ, rule = (list(it) + ["", "", ""])[:3]
            out.append((str(name), str(typ or "text"), str(rule or "（未记录规则）")))
        else:
            rule = ""
            if hasattr(it, "describe_rule"):
                try:
                    rule = it.describe_rule()
                except Exception:
                    rule = ""
            out.append((str(getattr(it, "name", "?")),
                        str(getattr(it, "type", "text") or "text"),
                        rule or "（未记录规则）"))
    return out


@dataclass
class Report:
    captured: Captured
    result: ParseResult
    card: str = ""
    engine: str = ""
    pages: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- headline
    def headline(self) -> str:
        ch = self.captured.channel or "-"
        eng = self.engine or self.captured.engine or "HTTP"
        res = self.result
        return (f"{ch} ｜ {eng} ｜ {res.level or 'card'} ｜ "
                f"{len(res.rows)} 行 × {len(res.fields)} 列 ｜ "
                f"未识别 {len(res.unparsed)} 行" +
                (f" ｜ {self.pages} 页" if self.pages > 1 else ""))

    # ------------------------------------------------------------- fill rates
    def thin_fields(self, threshold: float = 0.8) -> list[tuple[str, float]]:
        return [(f, r) for f, r in self.result.fill_rates.items() if r < threshold]

    def fill_block(self) -> list[str]:
        rates = self.result.fill_rates
        if not rates:
            return []
        parts = [f"{f} {fmt_pct(r)}" for f, r in rates.items()]
        lines = ["字段填充：" + " ｜ ".join(parts)]
        thin = self.thin_fields()
        if thin:
            lines.append("⚠ 偏低：" + "、".join(f"{f} {fmt_pct(r)}" for f, r in thin)
                         + " —— 该字段规则可能失效，或这类记录本来就没有这个值")
        return lines

    # ------------------------------------------------------- why it is empty
    def emptiness(self) -> dict[str, dict[str, int]]:
        """field -> {status: count}, only for cells that came out empty.

        One empty cell used to mean four different things. Grouping by status
        turns "客户名 62%" into "客户名 62%：其中 38% 是正则没匹配上" -- which
        points at the card, not at the page.
        """
        out: dict[str, dict[str, int]] = {}
        for row in self.result.rows:
            for name in self.result.fields:
                if _filled(row.values.get(name)):
                    continue
                t = row.traces.get(name)
                status = t.status if t is not None else "no-trace"
                out.setdefault(name, {})
                out[name][status] = out[name].get(status, 0) + 1
        return out

    def field_block(self) -> list[str]:
        """Type, rule and dominant failure reason for every field.

        The rule is printed because a fill rate without its rule is a number the
        user cannot act on: they still have to open the card and guess which of
        four primitives is the broken one.
        """
        specs = _specs(self.extra.get("field_specs") or [])
        if not specs:
            return []
        empty = self.emptiness()
        total = len(self.result.rows) or 1
        lines = ["字段定义："]
        for name, typ, rule in specs:
            bits = [name, typ, rule]
            bad = empty.get(name) or {}
            if bad:
                worst = max(bad.items(), key=lambda kv: kv[1])
                bits.append(f"空 {worst[1]}/{total} 条主要因为："
                            f"{_STATUS_CN.get(worst[0], worst[0])}")
            lines.append("  · " + " ｜ ".join(bits))
        return lines

    def explain_block(self, n: int = 3) -> list[str]:
        """Point at 第几页 / 哪条记录 / 哪个字段 / 哪条规则.

        Only rows that HAVE a problem are listed, and only the first few: a
        report that lists every empty cell is a report nobody reads.
        """
        bad: list[str] = []
        for i, row in enumerate(self.result.rows, 1):
            for name in self.result.fields:
                if _filled(row.values.get(name)):
                    continue
                t = row.traces.get(name)
                if t is None:
                    continue
                page = f"第 {row.source_page} 页 " if row.source_page else ""
                bad.append(f"  {page}第 {i} 条 · {t.describe()}"
                           + (f" ｜ 规则 {t.rule}" if t.rule else ""))
                break
            if len(bad) >= n:
                break
        if not bad:
            return []
        return [f"空值定位（最多列 {n} 条）："] + bad

    # ------------------------------------------------------------- integrity
    def integrity(self) -> list[str]:
        out: list[str] = []
        problem = self.result.check_conservation()
        if problem:
            out.append("✗ " + problem)
        elif self.result.total_lines:
            out.append(f"✓ 行数守恒：{self.result.total_lines} 行全部有归属"
                       f"（记录占用 {self.result.consumed_lines} 行 + 未识别 "
                       f"{len(self.result.unparsed)} 行）")
        if self.result.unparsed:
            out.append(f"  未识别的 {len(self.result.unparsed)} 行不会被丢弃："
                       f"导出时一并写到同名 .unparsed.txt")
        return out

    def deviation_block(self) -> list[str]:
        """What the card expected and this page did not deliver.

        Printed as a warning, never as a filter: the page is still parsed, still
        counted, still exported. The point is that a layout change shows up as a
        sentence in the report instead of as columns that look fine and are
        filled from the wrong lines.
        """
        if not self.result.deviations:
            return []
        out = ["⚠ 结构偏离（卡里声明的记录形态，这一页没满足）："]
        out.extend("  · " + d for d in self.result.deviations)
        out.append("  这些记录仍然保留并导出；请核对上面几条，或更新卡的约束")
        return out

    def lines(self) -> list[str]:
        # When a channel had to be chosen, that choice is the first thing the
        # report says. A user who thinks they are hitting an API and is really
        # driving their browser is being lied to by a report that only says
        # "it worked".
        head = self.extra.get("channel_decision")
        body = ([head] if head else []) + [self.headline()]
        body += self.provenance_block()
        body += self.fill_block() + self.integrity()
        body += self.deviation_block()
        body += self.field_block()
        body += self.explain_block()
        body.extend("· " + n for n in self.result.notes)
        return body

    def provenance_block(self) -> list[str]:
        """Where the bytes actually came from, and whether that is provable.

        Printed on every run, not only on suspicious ones: a provenance line
        that shows up only when something is wrong is itself a signal, and the
        user has no way to learn that the absence of the line means "fine".
        """
        cap = self.captured
        req = cap.requested_url or ""
        obs = cap.observed_url or ""
        if cap.channel == "desktop" and not obs:
            ident = cap.meta.get("target_title") or cap.source_url or "（未知窗口）"
            return [f"来源：未知地址 ｜ 实际驱动的是窗口 {ident[:50]!r}"
                    f"（桌面通道读不到地址栏，请自行确认这就是目标页）"]
        if obs and req and obs != req:
            return [f"来源：{obs[:90]}（发出的请求是 {req[:60]!r}，"
                    f"落地地址与请求不同）"]
        if obs:
            return [f"来源：{obs[:100]}"]
        return [f"来源：未知（{cap.channel or '无通道'} 没有提供可核对的地址）"]

    def render(self) -> str:
        return "\n".join(self.lines())

    def as_dict(self) -> dict[str, Any]:
        extra = {k: v for k, v in self.extra.items() if k not in _INTERNAL}
        specs = _specs(self.extra.get("field_specs") or [])
        return {
            "headline": self.headline(),
            "channel": self.captured.channel,
            "engine": self.engine or self.captured.engine,
            "level": self.result.level,
            "rows": len(self.result.rows),
            "fields": self.result.fields,
            "fill_rates": self.result.fill_rates,
            "empty_reasons": self.emptiness(),
            "field_rules": [{"name": n, "type": t, "rule": r} for n, t, r in specs],
            "unparsed": len(self.result.unparsed),
            "total_lines": self.result.total_lines,
            "consumed_lines": self.result.consumed_lines,
            "conservation": self.result.check_conservation(),
            "deviations": self.result.deviations,
            "deviation_rows": self.result.deviation_rows,
            "pages": self.pages,
            "notes": self.result.notes,
            "provenance": self.captured.provenance(),
            **extra,
        }


def sample_block(result: ParseResult, n: int = 3) -> list[str]:
    """The first few parsed rows, printed so a human can spot a wrong cut."""
    if not result.rows:
        return ["（一条记录都没切出来）"]
    out = [f"前 {min(n, len(result.rows))} 条："]

    def short(v: Any, width: int = 40) -> str:
        if v is None:
            return "∅"
        s = str(v).replace("\n", "⏎")
        return s if len(s) <= width else s[: width - 1] + "…"

    for i, r in enumerate(result.rows[:n], 1):
        cells = " ｜ ".join(f"{f}={short(r.values.get(f))}" for f in result.fields)
        out.append(f"  {i}. {cells}")
    return out
