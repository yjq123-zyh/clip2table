"""segment: the record boundary is where the architecture spends its budget.

A wrong boundary does not corrupt one cell - it merges or splits records, and
every later stage then faithfully processes garbage. These tests pin down the
cases a single-line length rule gets wrong.
"""
from __future__ import annotations

import unittest

from clip2table.pipeline.segment import (
    anchor_period,
    classify_line,
    segment,
    segment_auto,
)

# A record shaped like most list pages: title / date / body / spec / count.
BLOCK = [
    "星海无线耳机 Pro", "2026-03-14", "包装完好，降噪比上一代强不少，通勤地铁里基本听不到报站",
    "规格：黑色 / 降噪版", "有用(128)",
]


def blocks(n: int, sep: str = "") -> list[str]:
    out: list[str] = []
    for i in range(n):
        if i and sep:
            out.append(sep)
        out.extend([x if k != 0 else f"{x} {i}" for k, x in enumerate(BLOCK)])
    return out


class TestAnchorNeedsTwoLines(unittest.TestCase):
    """A short line alone is not a record start. Two-line agreement is."""

    ANCHOR = r"^(?!\s).{1,40}$"
    # A record's date line is a BARE date. Anchoring on a loose date prefix
    # instead ("^\d{4}-\d{1,2}") lets a body line that merely starts with a
    # date become a record start - see test_loose_next_is_a_known_trap.
    NEXT = (r"^\d{4}\s*[-/.年]\s*\d{1,2}"
            r"\s*(?:[-/.月]\s*\d{1,2}\s*[日号]?)?\s*$")

    def test_one_record_with_a_short_body_line(self):
        # The body line "规格：黑色 / 降噪版" is short and would pass a pure
        # length test. The next line ("有用(128)") does not look like a date,
        # so the pair test rejects it. A single-line rule splits this into 2.
        lines = ["山雾便携咖啡机", "2026-03-08", "一般", "有用(37)"]
        seg = segment(lines, anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(seg.records), 1)
        self.assertEqual(seg.records[0][0], "山雾便携咖啡机")

    def test_body_line_containing_a_date_does_not_start_a_record(self):
        # A record whose body mentions a date in the middle. The next line
        # after the body is not a date, so this stays one record.
        lines = ["轻羽人体工学椅", "2026-03-11", "腰托到位", "2026-03-12 补记：扶手略窄", "有用(56)"]
        seg = segment(lines, anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(seg.records), 1)
        self.assertEqual(len(seg.records[0]), 5)

    def test_three_records(self):
        seg = segment(blocks(3), anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(seg.records), 3)
        self.assertTrue(all(len(r) == 5 for r in seg.records))

    def test_blank_lines_between_records_are_not_records(self):
        seg = segment(blocks(3, sep=""), anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(seg.records), 3)

    def test_lines_before_the_first_anchor_are_unparsed_not_dropped(self):
        lines = ["导出时间 2026-03-20", "共 3 条"] + blocks(2)
        seg = segment(lines, anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(seg.records), 2)
        self.assertEqual(seg.unparsed, ["导出时间 2026-03-20", "共 3 条"])

    def test_no_anchor_hit_is_all_unparsed(self):
        lines = ["1", "2", "3"]
        seg = segment(lines, anchor=r"^★", anchor_next=r".")
        self.assertEqual(seg.records, [])
        self.assertEqual(seg.unparsed, ["1", "2", "3"])

    def test_loose_next_is_a_known_trap(self):
        # Why NEXT is anchored with "$": a record's own body line may START
        # with a date ("2026-03-12 补记：..."). Anchoring on a loose date
        # prefix makes that body line look like the next record's date, so the
        # line before it starts a phantom record. This test pins the failure
        # mode so nobody "simplifies" NEXT back to a prefix match.
        lines = ["轻羽人体工学椅", "2026-03-11", "腰托到位",
                 "2026-03-12 补记：扶手略窄", "有用(56)"]
        loose = segment(lines, anchor=self.ANCHOR,
                        anchor_next=r"^\d{4}\s*[-/.年]\s*\d{1,2}")
        self.assertEqual(len(loose.records), 2)   # the trap, demonstrated
        strict = segment(lines, anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(strict.records), 1)  # the rule that holds

    def test_single_line_anchor_is_not_enough(self):
        # Length alone cannot tell a title from a two-word body line. This is
        # the bug the old script had: one record became two.
        lines = ["山雾便携咖啡机", "2026-03-08", "一般", "有用(37)"]
        alone = segment(lines, anchor=self.ANCHOR)
        self.assertEqual(len(alone.records), 4)   # naive: every short line
        paired = segment(lines, anchor=self.ANCHOR, anchor_next=self.NEXT)
        self.assertEqual(len(paired.records), 1)  # two-line agreement: 1

    def test_bad_regex_does_not_raise(self):
        seg = segment(["a", "b"], anchor="([", anchor_next=".")
        self.assertEqual(seg.records, [])
        self.assertEqual(len(seg.unparsed), 2)
        self.assertTrue(any("非法" in n for n in seg.notes))


class TestBlankSeparation(unittest.TestCase):
    def test_blank_line_splits(self):
        seg = segment(["a", "b", "", "c", "d", ""], blank_separated=True)
        self.assertEqual(seg.records, [["a", "b"], ["c", "d"]])

    def test_consecutive_blanks_do_not_make_empty_records(self):
        seg = segment(["a", "", "", "", "b", ""], blank_separated=True)
        self.assertEqual(seg.records, [["a"], ["b"]])


class TestConservation(unittest.TestCase):
    def test_every_line_is_accounted_for(self):
        lines = blocks(4)
        seg = segment(lines, anchor=TestAnchorNeedsTwoLines.ANCHOR,
                      anchor_next=TestAnchorNeedsTwoLines.NEXT)
        accounted = seg.consumed_lines + len(seg.unparsed)
        self.assertEqual(accounted, seg.total_lines)


class TestClassifyLine(unittest.TestCase):
    def test_shapes(self):
        cases = {
            "2026-03-14": "date",
            "2026年3月14日": "date",
            "规格：黑色": "kv",
            "1. 第一条": "bullet",
            "· 要点": "bullet",
            "（2）第二条": "bullet",
            "短标题": "short",
            "这一行写得非常长" * 7: "long",
            "": "empty",
            "   ": "empty",
        }
        for line, want in cases.items():
            self.assertEqual(classify_line(line), want, line)

    def test_a_url_is_not_a_kv(self):
        # "https://..." must not be read as key "https" + value "//...", or
        # an entire column would get named after a URL scheme.
        self.assertEqual(classify_line("https://example.com/a"), "short")


class TestAnchorPeriod(unittest.TestCase):
    def test_detects_period_of_uniform_blocks(self):
        period, records = anchor_period(blocks(4, sep=""))
        self.assertEqual(period, 5)
        self.assertEqual(records, 4)

    def test_too_few_lines_is_no_signal(self):
        self.assertEqual(anchor_period(["a", "b", "c", "d", "e"]), (0, 0))

    def test_irregular_text_is_no_signal(self):
        lines = ["a", "b" * 60, "c", "规格：d", "e" * 70, "f", "规格：g", "h"]
        self.assertEqual(anchor_period(lines)[0], 0)

    def test_tolerates_one_odd_line(self):
        # Real blocks are not perfectly uniform: one body line runs long. The
        # structure still repeats, and the boundary follows structure.
        lines = blocks(4, sep="")
        lines[2] = "这一行突然特别长" * 8
        self.assertEqual(anchor_period(lines)[0], 5)


class TestAnchorPeriodNeedsContrast(unittest.TestCase):
    """Repetition is not structure.

    A page whose lines are almost all short text repeats with period 2 by
    arithmetic, but nothing about it is periodic: whichever offset you stand on,
    the line is short. Cutting it into two-column records invents structure that
    was never there. This is measured, not hypothetical - a real site copied
    from a browser gave 115 short lines out of 119 and a confident period of 2,
    producing 60 fabricated two-column records out of a category menu.
    """

    def test_all_short_lines_are_not_a_period(self):
        menu = [f"分类 {i}" for i in range(40)]
        self.assertEqual(anchor_period(menu), (0, 0))

    def test_all_kv_lines_without_key_alternation_are_not_a_period(self):
        # Same key at both offsets: the offsets do not differ, so this is a
        # list of one shape, not a two-line record.
        flat = [f"备注：第 {i} 条" for i in range(20)]
        self.assertEqual(anchor_period(flat), (0, 0))

    def test_alternating_key_names_are_a_period(self):
        # The key name is on screen and distinguishes the offsets, so this IS a
        # two-line record: name then price, repeated.
        lines = []
        for i in range(6):
            lines += [f"名称：编号{i}", f"价格：{i}"]
        self.assertEqual(anchor_period(lines)[0], 2)

    def test_scattered_rare_lines_do_not_dominate(self):
        # A page of flat list text with two odd lines in it. Each offset group
        # is dominated by whichever shape happens to be commoner inside it, so
        # the offsets look different - but the shapes cover only about half to
        # two thirds of their groups. That is a bag of leftovers, not a position
        # in a repeating shape.
        #
        # The spacing is swept on purpose: a single spacing would only prove
        # that one arithmetic accident was caught. Whatever the gap, two dates
        # in forty flat lines peak at purity 0.667, below the 0.7 floor.
        for gap in (3, 5, 8, 11):
            with self.subTest(gap=gap):
                lines = [f"项目名称第 {i} 号" for i in range(40)]
                lines[3] = "2026-01-01"
                lines[3 + gap] = "2026-02-02"
                self.assertEqual(anchor_period(lines)[0], 0)

    def test_three_evenly_spaced_dates_are_indistinguishable_from_records(self):
        # Honest record of a case this heuristic does NOT reject. Three dates at
        # a fixed offset in flat text look exactly like three five-line records
        # whose k-th line is a date - that is what a real record looks like, and
        # no local evidence separates them. Asserted so the behaviour is pinned:
        # if a future change starts rejecting this, it must be a deliberate one,
        # and the trade-off (rejecting real 5-line records) must be weighed.
        lines = [f"项目名称第 {i} 号" for i in range(40)]
        for i in (3, 8, 13):
            lines[i] = f"2026-01-0{i}"
        self.assertEqual(anchor_period(lines)[0], 5)


class TestSegmentAuto(unittest.TestCase):
    def test_anchored_puts_the_title_first(self):
        seg = segment_auto(blocks(3, sep=""), "anchored")
        self.assertEqual(len(seg.records), 3)
        self.assertTrue(seg.records[0][0].startswith("星海无线耳机"))

    def test_lines_level_is_one_record_per_line(self):
        seg = segment_auto(["a", "b", "c"], "lines")
        self.assertEqual(seg.records, [["a"], ["b"], ["c"]])


if __name__ == "__main__":
    unittest.main()
