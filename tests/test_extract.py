"""extract: seven primitives, no eighth, and never a value the source lacks.

The invariant under test throughout: a field the source does not contain comes
back None. Empty is a fact, and a fact worth keeping - the only thing worse
than a missing value is an invented one.
"""
from __future__ import annotations

import unittest

from clip2table.pipeline.extract import FieldSpec, KINDS, TYPES, extract

RECORD = [
    "星海无线耳机 Pro",
    "2026-03-14",
    "包装完好，降噪比上一代强不少",
    "规格：黑色 / 降噪版",
    "有用(128)",
]


class TestLinePrimitive(unittest.TestCase):
    def test_positive_index(self):
        row = extract(RECORD, [FieldSpec(name="t", kind="line", index=0)])
        self.assertEqual(row.get("t"), "星海无线耳机 Pro")

    def test_negative_index_counts_from_the_end(self):
        row = extract(RECORD, [FieldSpec(name="t", kind="line", index=-1)])
        self.assertEqual(row.get("t"), "有用(128)")

    def test_out_of_range_is_none_not_an_error(self):
        row = extract(RECORD, [FieldSpec(name="t", kind="line", index=99)])
        self.assertIsNone(row.get("t"))

    def test_empty_record_is_none(self):
        row = extract([], [FieldSpec(name="t", kind="line", index=0)])
        self.assertIsNone(row.get("t"))


class TestSearchPrimitive(unittest.TestCase):
    def test_named_group_wins(self):
        spec = FieldSpec(name="d", kind="search", pattern=r"(?P<v>\d{4}-\d{2}-\d{2})")
        self.assertEqual(extract(RECORD, [spec]).get("d"), "2026-03-14")

    def test_first_group_wins_when_unnamed(self):
        spec = FieldSpec(name="n", kind="search", pattern=r"有用[（(](\d+)")
        self.assertEqual(extract(RECORD, [spec]).get("n"), "128")

    def test_whole_match_when_no_group(self):
        spec = FieldSpec(name="s", kind="search", pattern=r"规格：\S+")
        self.assertEqual(extract(RECORD, [spec]).get("s"), "规格：黑色")

    def test_no_match_is_none(self):
        spec = FieldSpec(name="x", kind="search", pattern=r"不存在")
        self.assertIsNone(extract(RECORD, [spec]).get("x"))

    def test_min_len_rejects_a_too_short_capture(self):
        # Body line 3 is long; the short line "好" is not a usable comment.
        rec = ["标题", "好", "这是一段足够长的正文内容"]
        spec = FieldSpec(name="c", kind="search", pattern=r"^(.+)$", min_len=6)
        self.assertEqual(extract(rec, [spec]).get("c"), "这是一段足够长的正文内容")

    def test_exclude_rejects_known_ui_noise(self):
        # Every line matches, but the first one is a UI label not a value.
        rec = ["有用 12", "这台机器确实安静"]
        spec = FieldSpec(name="c", kind="search", pattern=r"^(.+)$", exclude=["有用"])
        self.assertEqual(extract(rec, [spec]).get("c"), "这台机器确实安静")

    def test_scope_head_limits_the_window(self):
        spec = FieldSpec(name="t", kind="search", pattern=r"^(.+)$",
                         scope="head", window=1)
        self.assertEqual(extract(RECORD, [spec]).get("t"), "星海无线耳机 Pro")

    def test_scope_tail_limits_the_window(self):
        spec = FieldSpec(name="n", kind="search", pattern=r"^有用.+$", scope="tail",
                         window=1)
        self.assertEqual(extract(RECORD, [spec]).get("n"), "有用(128)")

    def test_a_tail_hit_reports_the_real_line_number(self):
        # RECORD has 5 lines; a tail window of 1 starts at line 4. Reporting 0
        # would say the value came from the top of the record, which is the
        # opposite of what the rule asked for.
        spec = FieldSpec(name="n", kind="search", pattern=r"^有用.+$", scope="tail",
                         window=1)
        row = extract(RECORD, [spec])
        self.assertEqual(row.traces["n"].line, len(RECORD) - 1)

    def test_a_tail_window_of_two_reports_the_right_offset(self):
        spec = FieldSpec(name="s", kind="search", pattern=r"^规格：(.+)$", scope="tail",
                         window=2)
        row = extract(RECORD, [spec])
        self.assertEqual(row.get("s"), "黑色 / 降噪版")
        self.assertEqual(row.traces["s"].line, len(RECORD) - 2)


class TestSpanPrimitive(unittest.TestCase):
    def test_span_to_end_of_record(self):
        spec = FieldSpec(name="b", kind="span", start="line:1")
        self.assertEqual(extract(RECORD, [spec]).get("b"),
                         "\n".join(RECORD[1:]))

    def test_span_between_two_literal_markers(self):
        spec = FieldSpec(name="b", kind="span",
                         start="after:2026-03-14", end="before:规格")
        self.assertEqual(extract(RECORD, [spec]).get("b"),
                         "包装完好，降噪比上一代强不少")

    def test_span_bound_referencing_an_earlier_field(self):
        specs = [
            FieldSpec(name="t", kind="line", index=0),
            FieldSpec(name="b", kind="span", start="after:t"),
        ]
        row = extract(RECORD, specs)
        self.assertEqual(row.get("b"), "\n".join(RECORD[1:]))

    def test_inverted_bounds_yield_none(self):
        # end comes before start: an empty span, not a reversed join.
        spec = FieldSpec(name="b", kind="span", start="line:3", end="line:1")
        self.assertIsNone(extract(RECORD, [spec]).get("b"))


class TestCellPrimitive(unittest.TestCase):
    def test_column_by_index(self):
        rec = ["A\tB\tC"]
        specs = [FieldSpec(name="a", kind="cell", col=0),
                 FieldSpec(name="c", kind="cell", col=2)]
        row = extract(rec, specs)
        self.assertEqual((row.get("a"), row.get("c")), ("A", "C"))

    def test_ragged_row_yields_none_not_shift(self):
        # A short row must not borrow the neighbouring cell's value.
        row = extract(["A\tB"], [FieldSpec(name="c", kind="cell", col=5)])
        self.assertIsNone(row.get("c"))


class TestConstPrimitive(unittest.TestCase):
    def test_const_returns_its_value(self):
        spec = FieldSpec(name="src", kind="const", value="京东")
        self.assertEqual(extract(RECORD, [spec]).get("src"), "京东")

    def test_const_requires_a_value(self):
        with self.assertRaises(ValueError):
            FieldSpec(name="src", kind="const")


class TestCoercion(unittest.TestCase):
    def test_int_strips_currency_and_separators(self):
        row = extract(["¥1,280"], [FieldSpec(name="p", kind="search",
                                            pattern=r"^(.+)$", type="int")])
        self.assertEqual(row.get("p"), 1280)

    def test_int_on_non_numeric_is_none(self):
        row = extract(["暂无"], [FieldSpec(name="p", kind="search",
                                          pattern=r"^(.+)$", type="int")])
        self.assertIsNone(row.get("p"))

    def test_float(self):
        # A trailing unit must be declared. "4.5 分" is a rating only because
        # the card says 分 belongs to this field; without that the conversion
        # cannot tell 分 from the word 分 in a sentence.
        row = extract(["4.5 分"], [FieldSpec(name="r", kind="search",
                                            pattern=r"^(.+)$", type="float",
                                            unit="分")])
        self.assertAlmostEqual(row.get("r"), 4.5)

    def test_undeclared_unit_is_a_conversion_failure(self):
        # "1.5万" is not 1.5. Guessing here produced a number the page never
        # stated and still reported `ok`, so the trace could not point at it.
        row = extract(["1.5万"], [FieldSpec(name="n", kind="search",
                                            pattern=r"^(.+)$", type="float")])
        self.assertIsNone(row.get("n"))
        self.assertEqual(row.traces["n"].status, "coerce-failed")

    def test_declared_unit_and_multiplier(self):
        row = extract(["1.5万"], [FieldSpec(name="n", kind="search",
                                            pattern=r"^(.+)$", type="float",
                                            unit="万", multiplier=10000)])
        self.assertAlmostEqual(row.get("n"), 15000.0)

    def test_range_is_rejected_by_default(self):
        # "10–20" has no single value. Taking the first fragment reported 10 as
        # if the page had said 10.
        row = extract(["10–20"], [FieldSpec(name="n", kind="search",
                                            pattern=r"^(.+)$", type="int")])
        self.assertIsNone(row.get("n"))
        self.assertEqual(row.traces["n"].status, "coerce-failed")

    def test_range_end_can_be_chosen_explicitly(self):
        for on_range, want in (("first", 10), ("last", 20)):
            row = extract(["10–20"], [FieldSpec(name="n", kind="search",
                                                pattern=r"^(.+)$", type="int",
                                                on_range=on_range)])
            self.assertEqual(row.get("n"), want, on_range)

    def test_exponent_is_not_silently_truncated(self):
        # "1e3" used to come back as 1.
        row = extract(["1e3"], [FieldSpec(name="n", kind="search",
                                          pattern=r"^(.+)$", type="float")])
        self.assertIsNone(row.get("n"))

    def test_percent_needs_the_flag(self):
        self.assertIsNone(extract(["45%"], [FieldSpec(
            name="p", kind="search", pattern=r"^(.+)$", type="float")]).get("p"))
        row = extract(["45%"], [FieldSpec(name="p", kind="search",
                                          pattern=r"^(.+)$", type="float",
                                          percent=True)])
        self.assertAlmostEqual(row.get("p"), 45.0)

    def test_impossible_calendar_date_is_not_normalized(self):
        # 2026-02-31 passed the old "1 <= d <= 31" check.
        row = extract(["2026-02-31"], [FieldSpec(name="d", kind="search",
                                                 pattern=r"^(.+)$", type="date")])
        self.assertIsNone(row.traces["d"].normalized)

    def test_bool_recognises_both_languages(self):
        for text, want in (("是", True), ("否", False), ("true", True), ("0", False)):
            row = extract([text], [FieldSpec(name="b", kind="search",
                                             pattern=r"^(.+)$", type="bool")])
            self.assertIs(row.get("b"), want, text)

    def test_date_is_kept_verbatim(self):
        # Interpreting 2026-03-14 into a datetime would be a decision this
        # layer has no business making: the caller's locale decides that.
        row = extract(["2026年3月14日"], [FieldSpec(name="d", kind="search",
                                                    pattern=r"^(.+)$", type="date")])
        self.assertEqual(row.get("d"), "2026年3月14日")

    def test_blank_becomes_none(self):
        row = extract(["   "], [FieldSpec(name="x", kind="search", pattern=r"^(.+)$")])
        self.assertIsNone(row.get("x"))


class TestBadPatternsFailClosed(unittest.TestCase):
    def test_invalid_regex_yields_none_not_a_traceback(self):
        # A card with a typo must not crash a 5000-row harvest at row 3000.
        spec = FieldSpec(name="x", kind="search", pattern="([")
        self.assertIsNone(extract(RECORD, [spec]).get("x"))


class TestDeclaredSetsAreClosed(unittest.TestCase):
    def test_seven_primitives_and_no_eighth(self):
        self.assertEqual(set(KINDS),
                         {"dom", "cell", "line", "search", "span", "json", "const"})

    def test_types(self):
        self.assertEqual(set(TYPES), {"text", "date", "int", "float", "str", "bool"})

    def test_unknown_kind_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            FieldSpec(name="x", kind="regex")

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            FieldSpec(name="x", kind="line", type="decimal")


if __name__ == "__main__":
    unittest.main()
