"""Tests for card regression (`clip2table test`).

The property that matters: a broken rule must FAIL, and it must fail by naming
the record number. `cards validate` cannot do that - a card with a legal but
wrong rule validates cleanly. That asymmetry is the reason this module exists,
so it is tested explicitly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from clip2table.cards.regress import _cell, _read_expect, test_card
from clip2table.cards.schema import parse_card
from clip2table.errors import InputError

import unittest

CARD = '''schema = 1
name = "t"
channel = "desktop"
samples = "s"

[segment]
mode = "blank"

[fields.title]
kind = "line"
index = 0

[fields.body]
kind = "line"
index = 1
'''

SAMPLE = """甲
正文甲

乙
正文乙
"""

EXPECT = "title\tbody\n甲\t正文甲\n乙\t正文乙\n"


def make_card(tmp: Path, card_text: str = CARD) -> object:
    """A card whose `samples` points at <tmp>/s, so paths stay in the sandbox."""
    p = tmp / "card.toml"
    p.write_text(card_text, encoding="utf-8")
    return parse_card({
        "schema": 1, "name": "t", "channel": "desktop", "samples": "s",
        "segment": {"mode": "blank"},
        "fields": {"title": {"kind": "line", "index": 0},
                   "body": {"kind": "line", "index": 1}},
    }, str(p))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sdir = self.tmp / "s"
        self.sdir.mkdir()
        self.card = make_card(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.sdir / name
        p.write_text(text, encoding="utf-8")
        return p


class TestPassesWhenCorrect(Base):
    def test_matching_sample_passes(self):
        self.write("a.txt", SAMPLE)
        self.write("a.expect.tsv", EXPECT)
        rep = test_card(self.card)
        self.assertTrue(rep.ok)
        self.assertEqual((rep.passed, rep.failed), (1, 0))
        self.assertEqual(rep.results[0].rows, 2)

    def test_bom_in_expect_file_is_tolerated(self):
        self.write("a.txt", SAMPLE)
        self.write("a.expect.tsv", "\ufeff" + EXPECT)
        self.assertTrue(test_card(self.card).ok)


class TestFailsByName(Base):
    def test_broken_rule_names_the_record_number(self):
        """The whole point: a legal-but-wrong rule is caught with its record no."""
        self.write("a.txt", SAMPLE)
        self.write("a.expect.tsv", EXPECT)
        broken = make_card(self.tmp, CARD)
        # same card, but title now reads the second line of each record
        broken.fields[0].index = 1
        rep = test_card(broken)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.failed, 1)
        f = rep.results[0].failures[0]
        self.assertEqual(f.index, 1)
        self.assertEqual(f.field, "title")
        self.assertEqual(f.expected, "甲")
        self.assertEqual(f.actual, "正文甲")

    def test_record_count_mismatch_is_reported(self):
        self.write("a.txt", SAMPLE)
        self.write("a.expect.tsv", "title\tbody\n甲\t正文甲\n")
        rep = test_card(self.card)
        self.assertFalse(rep.ok)
        r = rep.results[0]
        self.assertEqual((r.rows, r.expected_rows), (2, 1))
        self.assertTrue(any("记录数不一致" in p for p in r.problems))

    def test_header_mismatch_is_reported(self):
        self.write("a.txt", SAMPLE)
        self.write("a.expect.tsv", "title\tcontent\n甲\t正文甲\n乙\t正文乙\n")
        rep = test_card(self.card)
        self.assertFalse(rep.ok)
        self.assertTrue(any("表头" in p for p in rep.results[0].problems))

    def test_missing_expect_file_fails_the_sample(self):
        self.write("a.txt", SAMPLE)
        rep = test_card(self.card)
        self.assertFalse(rep.ok)
        self.assertTrue(any("缺期望文件" in p for p in rep.results[0].problems))


class TestRefusesToGuess(Base):
    def test_card_without_samples_key_is_an_input_error(self):
        card = parse_card({
            "schema": 1, "name": "t", "segment": {"mode": "blank"},
            "fields": {"a": {"kind": "line", "index": 0}},
        }, str(self.tmp / "c.toml"))
        with self.assertRaises(InputError) as cm:
            test_card(card)
        self.assertIn("samples", str(cm.exception))
        self.assertIn("samples/<", cm.exception.hint or "")

    def test_missing_samples_dir_names_the_wrong_value(self):
        self.card.samples = "nope"
        with self.assertRaises(InputError) as cm:
            test_card(self.card)
        self.assertIn("nope", str(cm.exception))

    def test_empty_samples_dir_is_an_error_not_a_pass(self):
        with self.assertRaises(InputError) as cm:
            test_card(self.card)
        self.assertIn("没有样本", str(cm.exception))


class TestCellEscaping(unittest.TestCase):
    def test_newline_is_escaped_so_rows_stay_one_line(self):
        self.assertEqual(_cell("a\nb"), "a\\nb")
        self.assertEqual(_cell("a\r\nb"), "a\\nb")
        self.assertEqual(_cell("a\rb"), "a\\nb")

    def test_none_and_empty_are_the_same(self):
        self.assertEqual(_cell(None), "")
        self.assertEqual(_cell(""), "")

    def test_whitespace_is_trimmed_on_both_sides(self):
        self.assertEqual(_cell("  x  "), "x")

    def test_round_trip_through_the_file(self):
        d = Path(tempfile.mkdtemp(prefix="c2t-expect-"))
        p = d / "x.expect.tsv"
        p.write_text("a\tb\n1\\n2\t\n", encoding="utf-8")
        header, rows = _read_expect(p)
        self.assertEqual(header, ["a", "b"])
        # cells stay escaped; _cell() escapes the parsed side the same way
        self.assertEqual(rows, [["1\\n2", ""]])


if __name__ == "__main__":
    unittest.main()
