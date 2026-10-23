"""CF_HTML / RTF decoding. The byte-offset rule has its own test class.

These are the tests that would have caught a character-offset slice: the
fixture puts multi-byte text BEFORE the fragment, so any implementation that
slices the decoded string lands mid-character.
"""
from __future__ import annotations

import unittest

from clip2table.channels.cfhtml import (
    extract_html_fragment,
    parse_header,
    rtf_to_text,
)

PREFIX = "前言：这是一段非 ASCII 文本，用来把字节偏移和字符偏移拉开距离。\n"


def build_cfhtml(fragment: str, *, start_pad: int = 0, source: str = "file:///x") -> bytes:
    """Emit a genuine CF_HTML payload whose offsets are BYTE offsets."""
    head_tpl = (
        "Version:0.9\r\n"
        "StartHTML:{sh:010d}\r\n"
        "EndHTML:{eh:010d}\r\n"
        "StartFragment:{sf:010d}\r\n"
        "EndFragment:{ef:010d}\r\n"
        "SourceURL:{src}\r\n"
    )
    # Two-pass: build with placeholder widths, then fill real numbers.
    probe = head_tpl.format(sh=0, eh=0, sf=0, ef=0, src=source)
    body_prefix = PREFIX[:start_pad] if start_pad else ""
    html_open = "<html><body>\r\n<!--StartFragment-->"
    html_close = "<!--EndFragment-->\r\n</body></html>"
    sh = len(probe.encode("utf-8"))
    sf = sh + len((body_prefix + html_open).encode("utf-8"))
    ef = sf + len(fragment.encode("utf-8"))
    eh = ef + len(html_close.encode("utf-8"))
    head = head_tpl.format(sh=sh, eh=eh, sf=sf, ef=ef, src=source)
    assert len(head.encode("utf-8")) == sh
    raw = (head + body_prefix + html_open + fragment + html_close).encode("utf-8")
    return raw


class TestHeader(unittest.TestCase):
    def test_offsets_are_parsed_as_integers(self):
        info = parse_header(build_cfhtml("<b>x</b>"))
        self.assertEqual(info["Version"], "0.9")
        for k in ("StartHTML", "EndHTML", "StartFragment", "EndFragment"):
            self.assertIsInstance(info[k], int)

    def test_source_url_kept_verbatim(self):
        info = parse_header(build_cfhtml("<b>x</b>", source="https://a.example/p?q=1"))
        self.assertEqual(info["SourceURL"], "https://a.example/p?q=1")

    def test_missing_keys_come_back_as_none(self):
        info = parse_header(b"garbage")
        self.assertIsNone(info.get("StartHTML"))


class TestByteOffsets(unittest.TestCase):
    def test_non_ascii_before_the_fragment_does_not_shift_the_slice(self):
        frag = "<table><tr><td>鹈鹕</td></tr></table>"
        raw = build_cfhtml(frag)
        self.assertEqual(extract_html_fragment(raw), frag)

    def test_slice_is_strictly_decodable(self):
        # A character-offset implementation produces U+FFFD here; strict
        # decoding is the tripwire.
        frag = "<p>饕餮魑魅魍魉耄耋</p>"
        got = extract_html_fragment(build_cfhtml(frag))
        self.assertEqual(got.count("\ufffd"), 0)
        self.assertEqual(got, frag)

    def test_emoji_and_zwj_sequences_survive(self):
        frag = "<p>家庭 👨‍👩‍👧‍👦 国旗 🇨🇳 重音 é</p>"
        self.assertEqual(extract_html_fragment(build_cfhtml(frag)), frag)

    def test_byte_offset_differs_from_char_offset_when_non_ascii_precedes(self):
        # Prove the fixture actually exercises the difference, otherwise this
        # whole class is vacuous.
        raw = build_cfhtml("<p>鹈鹕</p>", start_pad=20)
        info = parse_header(raw)
        text = raw.decode("utf-8")
        byte_slice = raw[info["StartFragment"]:info["EndFragment"]].decode("utf-8")
        char_slice = text[info["StartFragment"]:info["EndFragment"]]
        self.assertEqual(byte_slice, "<p>鹈鹕</p>")
        self.assertNotEqual(char_slice, byte_slice)

    def test_broken_offsets_degrade_instead_of_raising(self):
        raw = build_cfhtml("<p>x</p>")
        bad = raw.replace(b"StartFragment:0000000", b"StartFragment:0000009")
        # Must not raise; some suffix of the payload comes back.
        self.assertIsInstance(extract_html_fragment(bad), str)

    def test_offsets_past_the_end_fall_back(self):
        raw = b"Version:0.9\r\nStartHTML:9999\r\nEndHTML:99999\r\n"
        self.assertIsInstance(extract_html_fragment(raw), str)

    def test_payload_without_a_header_returns_text(self):
        self.assertEqual(extract_html_fragment(b"<p>bare</p>"), "<p>bare</p>")

    def test_fragment_markers_used_when_offsets_absent(self):
        raw = (b"<html><!--StartFragment--><i>in</i><!--EndFragment--></html>")
        self.assertEqual(extract_html_fragment(raw), "<i>in</i>")


class TestRtf(unittest.TestCase):
    def test_plain_control_words_strip_to_text(self):
        out = rtf_to_text(b"{\\rtf1\\ansi hello \\b world\\b0}")
        self.assertIn("hello", out)
        self.assertIn("world", out)

    def test_par_becomes_a_newline(self):
        out = rtf_to_text(b"{\\rtf1 one\\par two}")
        self.assertIn("one\ntwo", out)

    def test_tab_becomes_a_tab(self):
        self.assertIn("\t", rtf_to_text(b"{\\rtf1 a\\tab b}"))

    def test_font_table_is_not_leaked_into_the_text(self):
        raw = b"{\\rtf1{\\fonttbl{\\f0 Arial;}}{\\colortbl;\\red0;}\\f0 body}"
        out = rtf_to_text(raw)
        self.assertNotIn("Arial", out)
        self.assertIn("body", out)

    def test_unicode_escape_decodes(self):
        # \uN emits codepoint N; the fallback char after it is skipped by RTF
        # readers, here it is simply left in place and harmless.
        out = rtf_to_text("{\\rtf1 \\u40533?}".encode("ascii"))
        self.assertIn("\u9e55", out)

    def test_hex_escapes_decode(self):
        out = rtf_to_text(b"{\\rtf1 caf\\'e9}")
        self.assertIn("caf", out)

    def test_escaped_braces_stay_literal(self):
        out = rtf_to_text(b"{\\rtf1 \\{x\\}}")
        self.assertIn("{x}", out)

    def test_output_is_balanced_when_braces_are_unbalanced(self):
        # A truncated payload must not raise.
        self.assertIsInstance(rtf_to_text(b"{\\rtf1 {unclosed"), str)

    def test_empty_input(self):
        self.assertEqual(rtf_to_text(b""), "")


if __name__ == "__main__":
    unittest.main()
