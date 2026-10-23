"""Win32 clipboard reader. Read-only by design: this module never writes.

Why not tkinter: it cannot read CF_HTML at all (raises
`TclError: ... form "STRING" not defined`) and building a Tk root costs
95-109 ms per read. ctypes reads in 0.3 ms and gets HTML, RTF and TSV.

Every restype below is load-bearing. Without them 64-bit handles get
truncated and GetClipboardData returns a bogus pointer -> access violation.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from ..errors import ChannelError
from ..models import Captured
from .cfhtml import extract_html_fragment

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_TEXT = 1
CF_UNICODETEXT = 13

user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
CF_HTML = int(user32.RegisterClipboardFormatW("HTML Format"))
CF_RTF = int(user32.RegisterClipboardFormatW("Rich Text Format"))

user32.OpenClipboard.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]

kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [wintypes.HANDLE]


def sequence_number() -> int:
    """Changes whenever anything writes the clipboard. No window needed."""
    return int(user32.GetClipboardSequenceNumber())


def available_formats() -> list[tuple[int, str]]:
    """(format id, name) for everything currently on the clipboard."""
    out: list[tuple[int, str]] = []
    if not user32.OpenClipboard(None):
        raise ChannelError("打不开剪贴板：另一个程序正占用它",
                           hint="关掉正在复制/粘贴的程序后重试")
    try:
        fmt = 0
        while True:
            fmt = int(user32.EnumClipboardFormats(fmt))
            if fmt == 0:
                break
            buf = ctypes.create_unicode_buffer(256)
            n = user32.GetClipboardFormatNameW(fmt, buf, 256)
            out.append((fmt, buf.value if n else f"CF_{fmt}"))
    finally:
        user32.CloseClipboard()
    return out


def _raw(fmt: int) -> bytes | None:
    h = user32.GetClipboardData(fmt)
    if not h:
        return None
    p = kernel32.GlobalLock(h)
    if not p:
        return None
    try:
        size = int(kernel32.GlobalSize(h))
        return ctypes.string_at(p, size) if size > 0 else None
    finally:
        kernel32.GlobalUnlock(h)


def read_formats(fmts: tuple[int, ...]) -> dict[int, bytes]:
    if not user32.OpenClipboard(None):
        raise ChannelError("打不开剪贴板：另一个程序正占用它")
    try:
        got: dict[int, bytes] = {}
        for f in fmts:
            b = _raw(f)
            if b is not None:
                got[f] = b
        return got
    finally:
        user32.CloseClipboard()


def decode_text(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def read_clipboard(need_html: bool = True) -> Captured:
    """Read the clipboard as a Captured. Prefers HTML, falls back to text.

    Empty clipboard and unreadable clipboard are deliberately reported the
    same way: from the caller's point of view there is simply nothing there.
    """
    fmts = (CF_HTML, CF_UNICODETEXT, CF_TEXT) if need_html else (CF_UNICODETEXT, CF_TEXT)
    got = read_formats(fmts)

    names = []
    if CF_HTML in got:
        names.append("CF_HTML")
    if CF_UNICODETEXT in got:
        names.append("CF_UNICODETEXT")
    if CF_TEXT in got:
        names.append("CF_TEXT")

    html = got.get(CF_HTML)
    text = decode_text(got.get(CF_UNICODETEXT)) or decode_text(got.get(CF_TEXT))

    if not html and not text.strip():
        raise ChannelError("剪贴板是空的，或者里面没有文字",
                           hint="先在目标页面/程序里 Ctrl+A、Ctrl+C，然后再跑一次")

    if html:
        frag = extract_html_fragment(html)
        return Captured(kind="html", payload=frag, formats=names, channel="desktop",
                        engine=None, meta={"raw_html_len": len(html)})

    return Captured(kind="text", payload=text, formats=names, channel="desktop")
