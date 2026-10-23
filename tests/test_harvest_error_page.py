"""The harvest must not turn a browser error page into data.

A tab showing "this site can't be reached" is a healthy tab from the keyboard's
point of view: Ctrl+A selects the error text, Ctrl+C copies it, the clipboard
sequence number moves, and the parser turns the apology into rows. Nothing
fails. Measured on Edge 155, the error page differs from a real page in exactly
two ways: `location.href` is `chrome-error://chromewebdata/`, and
`#main-frame-error` exists. `readyState` is "complete" on both.
"""
from __future__ import annotations

import unittest

from clip2table.errors import ChannelError
from clip2table.harvest import _check_page_usable


class FakeCDP:
    def __init__(self, href: str) -> None:
        self.href = href
        self.calls: list[str] = []

    def eval(self, expr: str):
        self.calls.append(expr)
        return self.href


class TestErrorPageRefused(unittest.TestCase):
    def test_error_page_stops_before_the_keys(self):
        notes: list[str] = []
        with self.assertRaises(ChannelError) as ctx:
            _check_page_usable(FakeCDP("chrome-error://chromewebdata/"), notes)
        msg = str(ctx.exception)
        self.assertIn("网络错误页", msg)
        # The hint must say what to do, not just that something is wrong.
        self.assertIn("刷新", ctx.exception.hint)

    def test_normal_page_passes_silently(self):
        notes: list[str] = []
        _check_page_usable(FakeCDP("https://example.com/list?page=1"), notes)
        self.assertEqual(notes, [])

    def test_data_and_blank_urls_are_not_error_pages(self):
        for href in ("data:text/html,<p>1</p>", "about:blank",
                     "file:///D:/tmp/a.html", "http://127.0.0.1:8791/page/1"):
            with self.subTest(href=href):
                _check_page_usable(FakeCDP(href), [])

    def test_an_unreadable_location_is_not_treated_as_an_error_page(self):
        # If eval returns something unexpected the check must not fire: this is
        # a guard against a specific known page, not a general "looks wrong"
        # switch that could stop legitimate harvests.
        class Weird(FakeCDP):
            def eval(self, expr):
                return None

        _check_page_usable(Weird(""), [])


if __name__ == "__main__":
    unittest.main()
