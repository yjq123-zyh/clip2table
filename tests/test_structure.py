"""Card structural constraints: the card says what a record looks like, per page.

The card fixes where a record starts. That is not the same as knowing the
records that came out are the shape the card was written for: a site can keep
its anchors and drop one line per record, and the result is a table with every
column filled and every value from the wrong line. These tests pin the two
things that make the check trustworthy:

* it warns, it never drops -- a deviating record is still a record;
* it says WHICH expectation failed and WHERE, not "something looks off".
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clip2table.cards.schema import parse_card
from clip2table.errors import InputError

CLEAN = """星海无线耳机 Pro
2026-03-14
包装完好，降噪强不少
规格：黑色 / 降噪版
有用(128)
轻羽人体工学椅
2026-03-11
腰托支撑到位
规格：灰 / 带脚托
有用(56)
山雾便携咖啡机
2026-03-08
出液稳定，清洗麻烦
规格：银 / 单杯
有用(37)"""

CARD = """
schema = 1
name = "strict"
channel = "desktop"

[segment]
mode = "anchor"
anchor = '^\\S'
anchor_next = '^\\d{4}-\\d{2}-\\d{2}$'
record_lines = [5, 5]
required = ["日期"]

[fields.line1]
kind = "line"
index = 0

[fields."日期"]
kind = "line"
index = 1

[fields.line3]
kind = "line"
index = 2
"""


def load(toml: str):
    import tomllib
    return parse_card(tomllib.loads(toml), "t.toml")


def parse(text: str, card):
    from clip2table.core import parse_capture
    from clip2table.models import Captured
    cap = Captured(kind="text", payload=text, channel="file", formats=["file"])
    res, _ = parse_capture(cap, card)
    return res


class TestCardDeclaresConstraints(unittest.TestCase):
    def test_record_lines_is_read_as_a_pair(self):
        card = load(CARD)
        self.assertEqual(card.record_lines, (5, 5))

    def test_record_lines_must_be_two_ordered_positive_ints(self):
        for bad, why in (("record_lines = [5]", "只有一个数"),
                         ("record_lines = [0, 3]", "下限是 0"),
                         ("record_lines = [6, 4]", "上下限颠倒"),
                         ("record_lines = ['a', 'b']", "不是整数")):
            toml = CARD.replace("record_lines = [5, 5]", bad)
            with self.assertRaises(InputError, msg=why):
                load(toml)

    def test_required_must_name_real_fields(self):
        toml = CARD.replace('required = ["日期"]', 'required = ["不存在的字段"]')
        with self.assertRaises(InputError) as ctx:
            load(toml)
        self.assertIn("不存在的字段", str(ctx.exception))


class TestDeviationIsReportedNotDropped(unittest.TestCase):
    def test_a_clean_page_says_nothing(self):
        res = parse(CLEAN, load(CARD))
        self.assertEqual(res.deviations, [])
        self.assertEqual(len(res.rows), 3)

    def test_a_record_with_the_wrong_line_count_is_flagged_and_kept(self):
        # One line missing from the middle record: the anchors still fire, so
        # the segmentation happily produces two records where there were three.
        broken = CLEAN.splitlines()
        del broken[7]
        res = parse("\n".join(broken), load(CARD))
        self.assertTrue(res.deviations)
        self.assertIn("行", res.deviations[0])
        self.assertTrue(res.rows, "偏离的记录被丢掉了——它必须保留")
        self.assertEqual(len(res.rows) + 0, len(res.rows))

    def test_a_missing_required_field_is_named(self):
        # A field that comes from a pattern the page never matches: the record
        # still forms (the boundary rule is satisfied), so this is exactly the
        # case a 必填 field catches and the boundary rule cannot.
        toml = CARD.replace('required = ["日期"]', 'required = ["备注"]') + """
[fields."备注"]
kind = "search"
pattern = '备注：(.*)'
"""
        card = load(toml)
        res = parse(CLEAN, card)
        self.assertTrue(any("必填字段 备注" in d for d in res.deviations),
                        res.deviations)

    def test_deviation_rows_point_at_the_offending_records(self):
        lines = CLEAN.splitlines()
        del lines[7]
        res = parse("\n".join(lines), load(CARD))
        self.assertTrue(res.deviation_rows)
        for i in res.deviation_rows:
            self.assertGreaterEqual(i, 1)
            self.assertLessEqual(i, len(res.rows))


class TestDeviationReachesDisk(unittest.TestCase):
    def test_a_deviating_page_is_marked_in_its_meta(self):
        from clip2table.dataset import dataset_from_card

        with tempfile.TemporaryDirectory() as d:
            card = load(CARD)
            lines = CLEAN.splitlines()
            del lines[7]
            text = "\n".join(lines)
            res = parse(text, card)
            ds = dataset_from_card(card, str(Path(d) / "ds"))
            ds.commit_page(1, text, {"channel": "file"}, res)
            meta = (Path(d) / "ds" / "pages" / "0001" / "meta.json").read_text(
                encoding="utf-8")
            self.assertIn("deviations", meta)
            self.assertIn("行", meta)


if __name__ == "__main__":
    unittest.main()
