"""An export must not change the data quietly.

Two ways a file can disagree with the parse:
  * the format cannot hold the value (xlsx: 32767 chars, XML-forbidden chars)
  * the format needs escaping the writer forgot (markdown header with `|`)

Both used to be silent. The first is only acceptable if the report says which
cell and the untouched value is kept somewhere; the second is just a bug.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from clip2table.models import Row
from clip2table.render.text import write_md
from clip2table.render.xlsx import MAX_CELL, write_xlsx


def _rows(**vals):
    return [Row(values=dict(vals))]


class TestXlsxSaysWhatItChanged(unittest.TestCase):
    def test_a_lossless_export_reports_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = write_xlsx(_rows(标题="短", 内容="正常文本"),
                           ["标题", "内容"], Path(tmp) / "a.xlsx")
            self.assertTrue(r.lossless)
            self.assertEqual(r.changes, [])
            self.assertIsNone(r.companion)

    def test_a_truncated_cell_is_reported_with_row_and_field(self):
        long_text = "长" * (MAX_CELL + 500)
        with tempfile.TemporaryDirectory() as tmp:
            r = write_xlsx(_rows(标题="甲", 内容=long_text),
                           ["标题", "内容"], Path(tmp) / "a.xlsx")
            self.assertFalse(r.lossless)
            c = r.changes[0]
            self.assertEqual(c.kind, "truncated")
            self.assertEqual(c.row, 2)          # header is row 1
            self.assertEqual(c.field, "内容")
            self.assertEqual(c.kept_len, MAX_CELL)
            self.assertIn(str(MAX_CELL), c.detail)

    def test_the_full_value_survives_in_the_companion_file(self):
        long_text = "长" * (MAX_CELL + 500)
        with tempfile.TemporaryDirectory() as tmp:
            r = write_xlsx(_rows(内容=long_text), ["内容"], Path(tmp) / "a.xlsx")
            self.assertIsNotNone(r.companion)
            self.assertTrue(r.companion.exists())
            got = [json.loads(line) for line in
                   r.companion.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["row"], 2)
            self.assertEqual(got[0]["field"], "内容")
            # Byte-identical to what was parsed: the point is recovery.
            self.assertEqual(got[0]["value"], long_text)

    def test_the_written_cell_really_is_shorter(self):
        """Prove the truncation happened, not just that it was announced."""
        long_text = "长" * (MAX_CELL + 500)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.xlsx"
            write_xlsx(_rows(内容=long_text), ["内容"], p)
            with zipfile.ZipFile(p) as z:
                sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("长" * MAX_CELL, sheet)
            self.assertNotIn("长" * (MAX_CELL + 1), sheet)

    def test_xml_forbidden_characters_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = write_xlsx(_rows(内容="前\x00后"), ["内容"], Path(tmp) / "a.xlsx")
            self.assertFalse(r.lossless)
            self.assertEqual(r.changes[0].kind, "stripped")
            self.assertIn("XML", r.changes[0].detail)
            self.assertEqual(r.changes[0].original, "前\x00后")

    def test_a_short_control_character_is_recovered_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = write_xlsx(_rows(内容="前\x01后"), ["内容"], Path(tmp) / "a.xlsx")
            got = json.loads(r.companion.read_text(encoding="utf-8"))
            self.assertEqual(got["value"], "前\x01后")


class TestMarkdownHeaderIsEscaped(unittest.TestCase):
    def test_a_pipe_in_a_field_name_does_not_shift_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.md"
            write_md(_rows(**{"a|b": "值"}), ["a|b"], p)
            header = p.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(header, "| a\\|b |")
            # Header and separator must agree on the column count.
            self.assertEqual(header.count("|") - header.count("\\|"), 2)

    def test_a_newline_in_a_field_name_does_not_break_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.md"
            write_md(_rows(**{"a\nb": "值"}), ["a\nb"], p)
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)      # header, separator, one row
            self.assertIn("<br>", lines[0])


if __name__ == "__main__":
    unittest.main()
