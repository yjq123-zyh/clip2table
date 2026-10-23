"""One capability table, consulted by validation, dispatch and help text.

The rule this module exists to enforce: **a card may not declare something the
program cannot execute.** Before this table, `kind = "dom"` and `kind = "json"`
passed validation, ran, and returned None -- so "the page has no such element",
"the regex found nothing" and "this build cannot do that at all" all arrived at
the user as the same empty cell. An empty cell that means three different things
is not a missing value, it is a missing explanation.

Keeping the table in one place means the three consumers cannot drift: the schema
rejects with the same reason the dispatcher would give, and `cards show` prints
the same reason a help screen would.
"""
from __future__ import annotations

from typing import NamedTuple

from .errors import InputError


class Capability(NamedTuple):
    kind: str
    channels: tuple[str, ...]   # channels that can actually fill this kind
    note: str                   # what to use instead, or how it behaves
    implemented: bool = True


# Kinds that appear in KINDS but are not wired to any producer. They stay in the
# vocabulary so old cards fail loudly rather than silently producing empties;
# when a producer lands, flip `implemented` and add the channels.
CAPABILITIES: dict[str, Capability] = {
    "cell": Capability("cell", ("http", "browser", "desktop"),
                       "表格的一列（HTML 表格或 TSV），列边界是事实"),
    "line": Capability("line", ("http", "browser", "desktop"),
                       "记录的第 n 行，负数从末尾数"),
    "search": Capability("search", ("http", "browser", "desktop"),
                         "匹配正则的那一行，再取捕获组"),
    "span": Capability("span", ("http", "browser", "desktop"),
                       "两个标记之间的文本，可跨行"),
    "const": Capability("const", ("http", "browser", "desktop"),
                        "固定值，只用于标注来源，从不拿来填数据"),
    "dom": Capability(
        "dom", (), "还没实现。现在是空值，但空值和「页面上没有这个元素」"
                   "看起来一模一样。改用 line/search/span/cell —— 复制的文本里"
                   "一样有这些字段", implemented=False),
    "json": Capability(
        "json", (), "还没实现。现在是空值。若目标是 JSON 接口，先把它存成文件，"
                    "用 run --text-file 走文本路径", implemented=False),
}

CHANNEL_CAPS: dict[str, tuple[str, ...]] = {
    "http": ("cell", "line", "search", "span", "const"),
    "browser": ("cell", "line", "search", "span", "const"),
    "desktop": ("cell", "line", "search", "span", "const"),
}


def check_kind(kind: str, channel: str | None, where: str) -> None:
    """Refuse a field the runtime could not fill. Raises with a way forward."""
    cap = CAPABILITIES.get(kind)
    if cap is None:
        raise InputError(f"{where}: kind={kind!r} 不在可用原语里")
    if not cap.implemented:
        raise InputError(
            f"{where}: kind={kind!r} 还没实现，声明它只会得到空值",
            hint=cap.note)
    if channel and kind not in CHANNEL_CAPS.get(channel, CHANNEL_CAPS["http"]):
        usable = "、".join(CHANNEL_CAPS.get(channel, ()))
        raise InputError(
            f"{where}: kind={kind!r} 在 {channel} 通道上不可用",
            hint=f"{channel} 通道能用的原语是 {usable}；{cap.note}")


def unsupported_kinds() -> list[str]:
    """Kinds declared by the vocabulary but not wired up yet."""
    return [k for k, c in CAPABILITIES.items() if not c.implemented]


def describe() -> list[str]:
    """Human/agent-facing capability list, shared by doctor and cards show."""
    out = []
    for kind, cap in CAPABILITIES.items():
        where = "、".join(cap.channels) if cap.channels else "（无）"
        state = "" if cap.implemented else "  ← 未实现，卡的校验会直接拒绝"
        out.append(f"{kind:7s} {where:26s} {cap.note}{state}")
    return out
