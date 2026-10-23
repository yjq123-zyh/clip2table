"""normalize: the noise that would otherwise look like structure.

Each case here is a payload that really can come off the Windows clipboard and
that, left alone, silently joins or splits records.
"""
from __future__ import annotations

import unittest

from clip2table.pipeline.normalize import collapse_ws, normalize_text, to_lines


class TestLineEndings(unittest.TestCase):
    def test_bare_cr_becomes_a_line_break(self):
        # Win32 readback preserves whatever the source wrote. A bare CR left in
        # place merges two records into one line - the worst possible failure,
        # because both records then look intact.
        self.assertEqual(to_lines(normalize_text("a\r\nb\rc\nd")),
                         ["a", "b", "c", "d"])

    def test_crlf_collapses_to_one_break(self):
        self.assertEqual(to_lines(normalize_text("a\r\nb")), ["a", "b"])

    def test_empty_lines_survive(self):
        # Blank lines ARE structure: they are the blank-separated record boundary.
        self.assertEqual(to_lines(normalize_text("a\n\nb")), ["a", "", "b"])

    def test_empty_input(self):
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(to_lines(""), [])


class TestInvisibleJunk(unittest.TestCase):
    def test_zero_width_removed(self):
        # A zero-width space at the start of a line defeats a `^\S` anchor.
        self.assertEqual(normalize_text("a\u200bb\u200dc\u2060d\ufeffe"),
                         "abcde")

    def test_control_chars_dropped(self):
        self.assertEqual(normalize_text("a\x00b\x01c"), "abc")

    def test_vertical_tab_and_form_feed_become_breaks(self):
        # Word and PDF pastes use these as soft page breaks; dropping them
        # joins two records.
        self.assertEqual(to_lines(normalize_text("a\x0bb\x0cc")), ["a", "b", "c"])

    def test_tab_is_structure_and_survives(self):
        # Tab is the column separator for Excel/TSV. Removing it destroys S0.
        self.assertEqual(to_lines(normalize_text("a\tb")), ["a\tb"])


class TestSpacing(unittest.TestCase):
    def test_nbsp_and_ideographic_space_become_plain_space(self):
        self.assertEqual(normalize_text("a\u00a0b\u3000c"), "a b c")

    def test_trailing_whitespace_stripped(self):
        self.assertEqual(normalize_text("a   \nb\t\n"), "a\nb\n")

    def test_leading_whitespace_kept(self):
        # Indentation is evidence for `^(?!\s)` anchors; stripping it would
        # make every indented body line look like a record start.
        self.assertEqual(normalize_text("  a"), "  a")

    def test_unicode_nfc(self):
        self.assertEqual(normalize_text("e\u0301"), "\u00e9")

    def test_collapse_ws(self):
        self.assertEqual(collapse_ws("  a \t b  "), "a b")


if __name__ == "__main__":
    unittest.main()
