"""normalize: make line endings, whitespace and zero-width junk predictable.

Nothing here guesses at structure - it only removes noise that would otherwise
be mistaken for structure. The one structurally important job is turning bare
CR into LF, because Win32 clipboard readback preserves whatever the source
wrote (a\\r\\nb\\rc\\nd comes back byte-identical) and a bare CR left in place
silently merges two records into one line.
"""
from __future__ import annotations

import re
import unicodedata

# Zero-width and directional marks: invisible, but they break regex anchors.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_TRAILING_WS = re.compile(r"[ \t\u00a0\u3000]+$")
_NBSP = re.compile(r"[\u00a0\u2007\u202f\u3000]")

# Control characters with no printable meaning. \t and \n are structure and
# must survive; \x0b/\x0c are soft page breaks in Word/PDF pastes, so they
# become line breaks instead of vanishing (vanishing would join two records).
_CTRL_TO_NL = dict.fromkeys(map(ord, "\x0b\x0c"), "\n")
_CTRL_DROP = dict.fromkeys(
    [c for c in range(0x20) if c not in (0x09, 0x0a)] + [0x7f], None
)


def normalize_text(text: str, *, strip_trailing: bool = True) -> str:
    """Normalize a text blob into LF-separated lines with predictable spacing."""
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text)
    s = s.translate(_INVISIBLE)
    s = s.replace("\r\n", "\n").replace("\r", "\n")      # bare CR MUST go
    s = s.translate(_CTRL_TO_NL)
    s = s.translate(_CTRL_DROP)
    s = _NBSP.sub(" ", s)
    if strip_trailing:
        s = "\n".join(_TRAILING_WS.sub("", ln) for ln in s.split("\n"))
    return s


def to_lines(text: str) -> list[str]:
    """Split normalized text into lines, keeping empty lines (they are structure)."""
    if not text:
        return []
    return text.split("\n")


def collapse_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def normalize_html_text(html: str) -> str:
    """HTML -> plain text lines, preserving table cell boundaries as tabs.

    Uses html.parser only (stdlib). Tables become TSV-like lines so the same
    downstream code handles pages and clipboard HTML identically.
    """
    from .htmltable import html_to_lines
    return to_lines("\n".join(html_to_lines(html)))
