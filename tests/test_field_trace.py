"""FieldTrace: "empty" is four different facts, and each one names its cause.

The bug this file exists to prevent: a card whose rule points past the end of a
record, a card whose regex simply does not match, and a card whose value is
genuinely absent all rendered as the same blank cell. The user then "fixes" the
source page instead of the rule. Each status below must therefore stay
distinguishable and say where the failure happened.
"""
from __future__ import annotations

import unittest

from clip2table.models import FieldTrace
from clip2table.pipeline.extract import FieldSpec, extract

RECORD = [
    "星海无线耳机 Pro",
    "2026-03-14",
    "包装完好，降噪比上一代强不少",
]


def one(spec: FieldSpec, record=None):
    """Extract a single-field row and return its trace."""
    row = extract(RECORD if record is None else record, [spec])
    return row.traces[spec.name]


class TestOkTrace(unittest.TestCase):
    def test_value_carries_raw_line_and_rule(self):
        t = one(FieldSpec("名称", kind="line", index=0))
        self.assertEqual(t.status, "ok")
        self.assertFalse(t.empty)
        self.assertEqual(t.raw, "星海无线耳机 Pro")
        self.assertEqual(t.line, 0)
        self.assertIn("line", t.rule)

    def test_rule_is_recorded_verbatim_so_it_can_be_copied_back(self):
        t = one(FieldSpec("规格", kind="search", pattern=r"规格：(.+)"))
        self.assertIn("规格", t.rule)

    def test_const_field_is_ok_without_a_line(self):
        t = one(FieldSpec("来源", kind="const", value="手动采集"))
        self.assertEqual((t.status, t.raw, t.line), ("ok", "手动采集", None))


class TestOutOfRange(unittest.TestCase):
    """The rule points outside the record: a rule/source mismatch, not absence."""

    def test_index_past_the_end(self):
        t = one(FieldSpec("备注", kind="line", index=9))
        self.assertEqual(t.status, "out-of-range")
        self.assertTrue(t.empty)
        self.assertIsNone(t.raw)

    def test_detail_names_the_record_length_and_the_asked_line(self):
        t = one(FieldSpec("备注", kind="line", index=9))
        self.assertIn("3", t.detail)   # record length
        self.assertIn("9", t.detail)   # requested index

    def test_negative_index_is_resolved_from_the_end(self):
        t = one(FieldSpec("末行", kind="line", index=-1))
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "包装完好，降噪比上一代强不少")
        self.assertEqual(t.line, 2)

    def test_cell_column_past_the_end(self):
        t = one(FieldSpec("第三列", kind="cell", col=5), record=["a\tb"])
        self.assertEqual(t.status, "out-of-range")
        self.assertIn("2", t.detail)

    def test_cell_field_on_an_empty_record(self):
        t = one(FieldSpec("列", kind="cell", col=0), record=[])
        self.assertEqual(t.status, "out-of-range")


class TestNoMatch(unittest.TestCase):
    """Text was there; the pattern did not match it. Fix the regex, not the page."""

    def test_pattern_misses(self):
        t = one(FieldSpec("价格", kind="search", pattern=r"￥(\d+)"))
        self.assertEqual(t.status, "no-match")
        self.assertTrue(t.empty)

    def test_min_len_rejection_is_reported_as_no_match(self):
        t = one(FieldSpec("内容", kind="line", index=2, pattern=r"(.+)", min_len=50))
        self.assertEqual(t.status, "no-match")
        self.assertIn("50", t.detail)

    def test_exclude_hit_is_reported_as_no_match(self):
        t = one(FieldSpec("内容", kind="line", index=2, pattern=r"(.+)",
                          exclude=["包装"]))
        self.assertEqual(t.status, "no-match")
        self.assertIn("排除", t.detail)

    def test_line_pattern_keeps_only_the_marked_part(self):
        # A line field with a pattern means "this line, but only the match".
        t = one(FieldSpec("日期", kind="line", index=1,
                          pattern=r"(\d{4}-\d{2}-\d{2})"))
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "2026-03-14")


class TestCoerceFailed(unittest.TestCase):
    """Text was found but is not the declared type: say so, don't say missing."""

    def test_non_numeric_text_into_int(self):
        t = one(FieldSpec("数量", kind="line", index=0, type="int"))
        self.assertEqual(t.status, "coerce-failed")
        self.assertEqual(t.raw, "星海无线耳机 Pro")
        self.assertIn("int", t.detail)

    def test_coerce_failed_keeps_the_raw_text_so_it_can_be_seen(self):
        t = one(FieldSpec("数量", kind="line", index=2, type="int"))
        self.assertEqual(t.status, "coerce-failed")
        self.assertIsNotNone(t.raw)

    def test_numeric_text_into_int_is_ok_and_converted(self):
        t = one(FieldSpec("数量", kind="line", index=0, type="int"),
                record=["128", "x"])
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "128")


class TestMissing(unittest.TestCase):
    """A declared span bound that cannot be located is a rule/source mismatch.

    It used to fall back to "the whole record", which handed back every line as
    this field's value: filled, wrong, and indistinguishable from a good read.
    """

    def test_inverted_bounds_are_missing(self):
        t = one(FieldSpec("回复", kind="span", start="line:2", end="line:1"))
        self.assertEqual(t.status, "missing")
        self.assertIn("顺序反了", t.detail)

    def test_absent_start_marker_is_missing_not_the_whole_record(self):
        t = one(FieldSpec("回复", kind="span", start="after:商家回复"))
        self.assertEqual(t.status, "missing")
        self.assertIsNone(t.raw)

    def test_absent_end_marker_is_missing_not_the_whole_record(self):
        t = one(FieldSpec("回复", kind="span", end="before:没有这一行"))
        self.assertEqual(t.status, "missing")
        self.assertIsNone(t.raw)

    def test_present_markers_still_span_normally(self):
        t = one(FieldSpec("正文", kind="span", start="after:2026-03-14"))
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "包装完好，降噪比上一代强不少")


class TestRuleError(unittest.TestCase):
    def test_broken_regex_is_reported_as_a_rule_error(self):
        t = one(FieldSpec("坏", kind="search", pattern="[unclosed"))
        self.assertEqual(t.status, "rule-error")
        self.assertTrue(t.detail)


class TestDateNormalization(unittest.TestCase):
    """The value keeps what the page said; the normalized form is the extra fact."""

    def test_iso_date_is_kept_alongside_the_original(self):
        t = one(FieldSpec("日期", kind="line", index=1, type="date"))
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "2026-03-14")
        self.assertEqual(t.normalized, "2026-03-14")

    def test_chinese_date_is_normalized_without_rewriting_the_value(self):
        t = one(FieldSpec("日期", kind="line", index=0, type="date"),
                record=["2026年3月5日"])
        self.assertEqual(t.raw, "2026年3月5日")
        self.assertEqual(t.normalized, "2026-03-05")

    def test_ambiguous_text_gets_no_normalized_form(self):
        t = one(FieldSpec("日期", kind="line", index=0, type="date"),
                record=["上周三"])
        self.assertEqual(t.status, "ok")
        self.assertIsNone(t.normalized)


class TestDescribe(unittest.TestCase):
    def test_each_status_explains_itself(self):
        cases = {
            "no-match": "正则没匹配上",
            "out-of-range": "不存在",
            "coerce-failed": "但不是",
            "missing": "没有这条规则要的东西",
        }
        for status, phrase in cases.items():
            t = FieldTrace("字段", "line", status=status, detail="int")
            self.assertIn(phrase, t.describe(), msg=status)

    def test_describe_names_the_line_when_known(self):
        t = FieldTrace("字段", "line", status="no-match", line=3)
        self.assertIn("第 3 行", t.describe())

    def test_ok_trace_describe_is_not_a_failure_message(self):
        t = FieldTrace("字段", "line", status="ok", raw="甲")
        self.assertNotIn("规则本身报错", t.describe())


class TestRoundTrip(unittest.TestCase):
    def test_as_dict_and_from_dict_preserve_every_field(self):
        t = FieldTrace("价格", "search", status="coerce-failed", raw="面议",
                       line=2, detail="float", rule="search '￥(\\d+)'",
                       normalized=None)
        back = FieldTrace.from_dict("价格", t.as_dict())
        self.assertEqual(back, t)

    def test_from_dict_defaults_to_ok_when_status_is_absent(self):
        back = FieldTrace.from_dict("名称", {"kind": "line", "raw": "甲"})
        self.assertEqual(back.status, "ok")
        self.assertFalse(back.empty)


class TestOptionalBound(unittest.TestCase):
    """`before?:` / `after?:` say "this marker may be absent" out loud.

    The default is strict: a marker that vanished is a rule/source mismatch.
    A card that serves two page shapes (with and without a trailing 规格 line)
    declares the stop marker optional instead of relying on a fallback.
    """

    def test_optional_end_marker_absent_runs_to_end_of_record(self):
        t = one(FieldSpec("正文", kind="span", start="after:2026-03-14",
                          end="before?:规格"))
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.raw, "包装完好，降噪比上一代强不少")

    def test_optional_end_marker_present_still_cuts(self):
        rec = ["标题", "2026-03-14", "正文第一行", "规格：黑色"]
        t = one(FieldSpec("正文", kind="span", start="after:2026-03-14",
                          end="before?:规格"), record=rec)
        self.assertEqual(t.raw, "正文第一行")

    def test_optional_start_marker_absent_starts_at_record_top(self):
        t = one(FieldSpec("正文", kind="span", start="after?:商家回复"))
        self.assertEqual(t.status, "ok")
        self.assertIn("星海无线耳机 Pro", t.raw)

    def test_unknown_bound_head_is_rejected_at_construction(self):
        # A typo must fail when the card is validated, not silently yield empty
        # cells at harvest time.
        with self.assertRaises(ValueError):
            FieldSpec("正文", kind="span", start="befor:日期")

    def test_bare_question_mark_head_is_rejected(self):
        with self.assertRaises(ValueError):
            FieldSpec("正文", kind="span", start="?:日期")


if __name__ == "__main__":
    unittest.main()
