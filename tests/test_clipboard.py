"""Clipboard channel.

The module under test is read-only, so the tests must write the clipboard
themselves. Every test that touches the real clipboard saves the old contents
first and puts them back in tearDown: a test suite that eats the user's
clipboard is a bad citizen, and this one runs on the user's desktop.
"""
from __future__ import annotations

import ctypes
import time
import unittest
from ctypes import wintypes

from clip2table.channels.clipboard import (
    CF_HTML,
    CF_TEXT,
    CF_UNICODETEXT,
    available_formats,
    decode_text,
    read_clipboard,
    read_formats,
    sequence_number,
)
from clip2table.errors import ChannelError

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]

GMEM_MOVEABLE = 0x0002


def _get_text() -> str:
    # Retrying matters more here than elsewhere: if this silently returns "",
    # setUp records an empty clipboard as "the saved contents" and tearDown
    # then wipes what the user actually had. Losing data is worse than a
    # transient red.
    if not _open_clipboard_retrying():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _open_clipboard_retrying(attempts: int = 20) -> bool:
    """OpenClipboard fails while any other process has it open.

    That is a real, transient, third-party condition -- another app copying
    something at the same moment -- not a bug in this code. Without a retry the
    whole suite goes red for a reason that has nothing to do with the suite,
    which teaches people to ignore red. Bounded: ~1s total, then give up and
    let the test fail honestly.
    """
    for i in range(attempts):
        if user32.OpenClipboard(None):
            return True
        if i + 1 < attempts:
            time.sleep(0.05)
    return False


def _clipboard_usable() -> bool:
    """Whether this session has a clipboard at all.

    Headless CI and locked-down sessions have none. That is not a failure --
    there is simply nothing to test -- so the tests that need a real clipboard
    ask this first and skip. A red that nobody can fix is worse than a skip
    that says why: it teaches people to ignore red, which is how a real bug
    gets missed.
    """
    if not _open_clipboard_retrying(attempts=4):
        return False
    user32.CloseClipboard()
    return True


def _set_text(s: str) -> None:
    data = s.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard_retrying():
        raise RuntimeError("cannot open clipboard for test setup")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise RuntimeError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


class TestDecodeText(unittest.TestCase):
    def test_utf16_with_terminator(self):
        self.assertEqual(decode_text("你好".encode("utf-16-le") + b"\x00\x00"), "你好")

    def test_stops_at_the_first_nul(self):
        raw = "abc".encode("utf-16-le") + b"\x00\x00" + "ignore".encode("utf-16-le")
        self.assertEqual(decode_text(raw), "abc")

    def test_none_and_empty(self):
        self.assertEqual(decode_text(None), "")
        self.assertEqual(decode_text(b""), "")

    def test_non_ascii_survives(self):
        s = "鹈鹕饕餮魑魅魍魉 👨‍👩‍👧‍👦"
        self.assertEqual(decode_text(s.encode("utf-16-le") + b"\x00\x00"), s)

    def test_odd_length_does_not_raise(self):
        self.assertIsInstance(decode_text(b"a\x00b"), str)


class TestRealClipboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _clipboard_usable():
            raise unittest.SkipTest("no clipboard in this session")

    def setUp(self):
        self._saved = _get_text()

    def tearDown(self):
        try:
            _set_text(self._saved)
        except Exception:
            pass

    def test_sequence_number_changes_when_the_clipboard_is_written(self):
        before = sequence_number()
        _set_text("clip2table-seq")
        self.assertNotEqual(sequence_number(), before)

    def test_sequence_number_needs_no_window(self):
        self.assertIsInstance(sequence_number(), int)

    def test_available_formats_includes_unicode_text(self):
        _set_text("clip2table-fmt")
        names = [n for _, n in available_formats()]
        self.assertIn(CF_UNICODETEXT, [i for i, _ in available_formats()])
        self.assertTrue(any("CF_" in n or n for n in names))

    def test_roundtrip_plain_text(self):
        payload = "第一行\n第二行\t带制表符"
        _set_text(payload)
        cap = read_clipboard()
        self.assertEqual(cap.kind, "text")
        self.assertEqual(cap.payload, payload)
        self.assertIn("CF_UNICODETEXT", cap.formats)

    def test_roundtrip_non_ascii(self):
        payload = "鹈鹕饕餮 · 1,234.50 元"
        _set_text(payload)
        self.assertEqual(read_clipboard().payload, payload)

    def test_read_formats_returns_only_what_exists(self):
        _set_text("clip2table-one-fmt")
        got = read_formats((CF_UNICODETEXT, CF_HTML))
        self.assertIn(CF_UNICODETEXT, got)
        # Plain text write does not create HTML Format; absence is not an error.
        self.assertNotIn(CF_HTML, got)

    def test_empty_clipboard_raises_with_a_way_out(self):
        if not user32.OpenClipboard(None):
            self.skipTest("clipboard busy")
        try:
            user32.EmptyClipboard()
        finally:
            user32.CloseClipboard()
        with self.assertRaises(ChannelError) as ctx:
            read_clipboard()
        self.assertIn("Ctrl+A", ctx.exception.hint)

    def test_html_is_preferred_over_text_when_both_exist(self):
        # Write a CF_HTML payload by hand: a real copy from a browser emits
        # both formats at once, and HTML is the one that keeps table structure.
        frag = "<table><tr><td>a</td></tr></table>"
        head = ("Version:0.9\r\nStartHTML:{sh:010d}\r\nEndHTML:{eh:010d}\r\n"
                "StartFragment:{sf:010d}\r\nEndFragment:{ef:010d}\r\n")
        probe = head.format(sh=0, eh=0, sf=0, ef=0)
        sh = len(probe.encode())
        prefix = "<!--StartFragment-->"
        sf = sh + len(prefix.encode())
        ef = sf + len(frag.encode())
        eh = ef
        raw = (head.format(sh=sh, eh=eh, sf=sf, ef=ef) + prefix + frag).encode()

        if not user32.OpenClipboard(None):
            self.skipTest("clipboard busy")
        try:
            user32.EmptyClipboard()
            for fmt, blob in ((CF_UNICODETEXT, "a".encode("utf-16-le") + b"\x00\x00"),
                              (CF_HTML, raw + b"\x00")):
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(blob))
                p = kernel32.GlobalLock(h)
                ctypes.memmove(p, blob, len(blob))
                kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(fmt, h):
                    kernel32.GlobalFree(h)
        finally:
            user32.CloseClipboard()

        cap = read_clipboard()
        self.assertEqual(cap.kind, "html")
        self.assertIn("<table>", cap.payload)
        self.assertIn("CF_HTML", cap.formats)
        self.assertIn("raw_html_len", cap.meta)


class TestClipboardBusyRetry(unittest.TestCase):
    """A third party holding the clipboard must not turn the suite red."""

    def test_retries_then_succeeds(self):
        if not _clipboard_usable():
            self.skipTest("no clipboard in this session")
        calls = {"n": 0}
        real = user32.OpenClipboard

        def flaky(hwnd):
            calls["n"] += 1
            if calls["n"] < 3:
                return 0
            return real(hwnd)

        user32.OpenClipboard = flaky
        try:
            self.assertTrue(_open_clipboard_retrying(attempts=5))
        finally:
            user32.OpenClipboard = real
            user32.CloseClipboard()
        self.assertEqual(calls["n"], 3, "成功后不该继续重试")

    def test_gives_up_after_the_bound(self):
        real = user32.OpenClipboard
        calls = {"n": 0}

        def always_busy(hwnd):
            calls["n"] += 1
            return 0

        user32.OpenClipboard = always_busy
        try:
            self.assertFalse(_open_clipboard_retrying(attempts=4))
        finally:
            user32.OpenClipboard = real
        self.assertEqual(calls["n"], 4, "必须真的在次数上限处放弃，而不是无限等")

    def test_a_busy_clipboard_would_have_wiped_it(self):
        """Why _get_text retries too: a silent "" becomes the 'saved' value."""
        real = user32.OpenClipboard
        user32.OpenClipboard = lambda hwnd: 0
        try:
            self.assertEqual(_get_text(), "")
        finally:
            user32.OpenClipboard = real


if __name__ == "__main__":
    unittest.main()
