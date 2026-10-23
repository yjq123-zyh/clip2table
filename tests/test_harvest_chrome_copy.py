"""The copy that succeeds and is still wrong.

A browser only writes the clipboard for the page that owns keyboard focus. When
a programmatic activation leaves the page unfocused, Ctrl+A selects the address
bar instead: the clipboard changes, the read succeeds, and the tool reports a
one-row table of the wrong thing. These tests pin the detector that turns that
into an error.
"""

import unittest

from clip2table.harvest import _chrome_copy_reason
from clip2table.models import Captured


def _cap(payload: str, kind: str = "text") -> Captured:
    return Captured(kind=kind, payload=payload)


class TestAddressBarLooksLikeAPage(unittest.TestCase):
    def test_link_wrapping_the_title_is_the_address_bar(self):
        cap = _cap('<a href="http://127.0.0.1:8793/shell">梯子测试·公告列表</a>',
                   kind="html")
        self.assertIn("网址", _chrome_copy_reason(cap, "梯子测试·公告列表 - 个人 - Microsoft Edge"))

    def test_bare_url_in_the_text_flavour_is_the_address_bar(self):
        got = _chrome_copy_reason(_cap("http://x.test/a"), "anything - Edge")
        self.assertIn("网址", got)

    def test_tab_title_without_the_link_is_still_browser_ui(self):
        got = _chrome_copy_reason(_cap("梯子测试·公告列表"),
                                  "梯子测试·公告列表 - 个人 - Microsoft Edge")
        self.assertIn("标题", got)

    def test_title_match_ignores_the_trailing_profile_and_browser_name(self):
        for title in ("公告列表 - 个人 - Microsoft Edge",
                      "公告列表 - Microsoft Edge",
                      "公告列表"):
            self.assertTrue(_chrome_copy_reason(_cap("公告列表"), title), title)


class TestRealContentIsNotFlagged(unittest.TestCase):
    def test_short_html_page_is_fine(self):
        cap = _cap("<h1>real page</h1><p>body text here</p>", kind="html")
        self.assertEqual(_chrome_copy_reason(cap, "x - Edge"), "")

    def test_a_short_line_that_is_not_a_url_is_fine(self):
        self.assertEqual(_chrome_copy_reason(_cap("第一页"), "公告 - Edge"), "")

    def test_records_that_merely_contain_a_url_are_fine(self):
        text = "临江区\n2026-03-01\n详见 http://x.test/a 的说明\n预算 38.6 万元"
        self.assertEqual(_chrome_copy_reason(_cap(text), "公告 - Edge"), "")

    def test_empty_capture_is_not_blamed_on_the_browser(self):
        self.assertEqual(_chrome_copy_reason(_cap(""), "公告 - Edge"), "")

    def test_a_long_document_starting_with_a_link_is_fine(self):
        body = '<a href="http://x.test">目录</a>' + "<p>正文</p>" * 200
        self.assertEqual(_chrome_copy_reason(_cap(body, kind="html"), "公告 - Edge"), "")


if __name__ == "__main__":
    unittest.main()
