"""What an export actually did to the data.

An export is not a copy. xlsx has a hard 32767-character cell limit and XML
forbids control characters, so a value that parsed correctly can come out of
the file shorter than it went in. Doing that silently is the same failure this
project exists to prevent, one layer out: the table looks complete and is not.

So every writer returns the path AND what it changed, and the caller is
expected to say so. Anything dropped is kept in a companion file so the loss is
recoverable instead of merely disclosed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Change:
    """One cell that did not survive the format unchanged."""

    row: int                 # 1-based row in the file, header included
    field: str
    kind: str                # "truncated" | "stripped"
    detail: str
    original: str = ""       # the value as parsed, kept for the companion file
    kept_len: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "field": self.field, "kind": self.kind,
                "detail": self.detail, "kept_len": self.kept_len}


@dataclass
class RenderResult:
    """`path` plus everything the format had to do to the values."""

    path: Path
    changes: list[Change] = field(default_factory=list)
    companion: Path | None = None   # where the untouched values were put

    # Lets `str(result)` / `Path(result)` keep working for callers that only
    # ever wanted the filename.
    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    @property
    def lossless(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        if self.lossless:
            return f"{self.path.name}：内容与解析结果一致"
        bits = [f"{self.path.name}：{len(self.changes)} 处内容被格式改动"]
        if self.companion:
            bits.append(f"完整值见 {self.companion.name}")
        return "，".join(bits)


def lossless(path: str | Path) -> RenderResult:
    return RenderResult(path=Path(path))
