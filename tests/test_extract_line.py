"""A `line` field's pattern must actually be applied.

Measured defect: the schema accepted `pattern` on a `line` field, `FieldSpec`
stored it, and extraction never looked at it -- so `负责人：甲一` came back with
its label while the card asked for `甲一`. It reported 100% filled, because a
wrong value and a right value are both "filled".
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip2table.pipeline.extract import FieldSpec, extract  # noqa: E402

RECORD = [
    "2026-05-02 工单第 01 号 已完成",
    "负责人：甲一",
]


class TestLinePattern(unittest.TestCase):
    def test_pattern_strips_the_label(self):
        spec = FieldSpec(name="负责人", kind="line", index=1,
                         pattern=r"^负责人[:：]\s*(?P<v>.+)$")
        self.assertEqual(extract(RECORD, [spec]).values["负责人"], "甲一")

    def test_no_pattern_returns_the_whole_line(self):
        spec = FieldSpec(name="负责人", kind="line", index=1)
        self.assertEqual(extract(RECORD, [spec]).values["负责人"],
                         "负责人：甲一")

    def test_pattern_that_does_not_match_yields_none_not_the_line(self):
        # Falling back to the raw line would be the silent version of the bug:
        # the field would look filled while carrying the wrong text.
        spec = FieldSpec(name="负责人", kind="line", index=1,
                         pattern=r"^负责人[:：]\s*(?P<v>.+)$")
        row = extract(["只有一行"], [spec])
        self.assertIsNone(row.values["负责人"])

    def test_group_one_is_used_when_there_is_no_named_group(self):
        spec = FieldSpec(name="n", kind="line", index=0,
                         pattern=r"工单第\s*(\d+)\s*号")
        self.assertEqual(extract(RECORD, [spec]).values["n"], "01")

    def test_min_len_and_exclude_still_apply(self):
        spec = FieldSpec(name="负责人", kind="line", index=1,
                         pattern=r"^负责人[:：]\s*(?P<v>.+)$", min_len=8)
        self.assertIsNone(extract(RECORD, [spec]).values["负责人"])

    def test_negative_index_with_pattern(self):
        spec = FieldSpec(name="负责人", kind="line", index=-1,
                         pattern=r"^负责人[:：]\s*(?P<v>.+)$")
        self.assertEqual(extract(RECORD, [spec]).values["负责人"], "甲一")


if __name__ == "__main__":
    unittest.main()
