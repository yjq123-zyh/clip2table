"""assemble: dedupe, and the two invariants that catch two different failures.

L2 conservation catches LOST data: consumed + unparsed must equal the source.
L4 per-record expectations catch a record silently SPLIT in two - the failure
conservation cannot see, because splitting keeps every line accounted for.
"""
from __future__ import annotations

import unittest

from clip2table.models import Row
from clip2table.pipeline.assemble import (
    AssembleOptions,
    assemble,
    dedupe,
    merge_pages,
)
from clip2table.pipeline.extract import FieldSpec
from clip2table.pipeline.segment import Segmented

SPECS = [FieldSpec(name="title", kind="line", index=0),
         FieldSpec(name="body", kind="line", index=1)]


def seg_of(*records: list[str], unparsed: list[str] | None = None) -> Segmented:
    s = Segmented(records=[list(r) for r in records],
                  unparsed=list(unparsed or []),
                  total_lines=sum(len(r) for r in records) + len(unparsed or []),
                  method="card")
    return s


class TestConservationInvariant(unittest.TestCase):
    def test_books_balance(self):
        res = assemble(seg_of(["a", "b"], ["c", "d"]), SPECS)
        self.assertIsNone(res.check_conservation())
        self.assertEqual(res.consumed_lines, 4)
        self.assertEqual(res.total_lines, 4)

    def test_records_plus_unparsed_equals_source(self):
        res = assemble(seg_of(["a", "b"], ["c", "d"], unparsed=["页眉垃圾"]), SPECS)
        self.assertEqual(res.consumed_lines + len(res.unparsed), res.total_lines)

    def test_a_row_whose_fields_are_all_empty_is_not_dropped_silently(self):
        # The record has lines but none match any field. Dropping it would
        # lose data invisibly, so its lines move to unparsed and are kept.
        specs = [FieldSpec(name="x", kind="search", pattern=r"^ZZZ(.+)$")]
        res = assemble(seg_of(["nothing here"]), specs)
        self.assertEqual(res.rows, [])
        self.assertEqual(res.unparsed, ["nothing here"])
        self.assertIsNone(res.check_conservation())

    def test_conservation_reports_a_real_shortfall(self):
        # Deliberately break the books the way a bug would.
        seg = Segmented(records=[["a", "b"]], unparsed=[], total_lines=3, method="card")
        res = assemble(seg, SPECS)
        problem = res.check_conservation()
        self.assertIsNotNone(problem)
        self.assertIn("丢行", problem)

    def test_conservation_is_silent_for_empty_input(self):
        res = assemble(Segmented(total_lines=0), SPECS)
        self.assertIsNone(res.check_conservation())


class TestFillRates(unittest.TestCase):
    def test_rate_reflects_only_real_values(self):
        res = assemble(seg_of(["a", "b"], ["c"], ["d", "e"]), SPECS)
        rates = res.fill_rates
        self.assertEqual(rates["title"], 1.0)
        self.assertAlmostEqual(rates["body"], 2 / 3)

    def test_all_empty_when_no_rows(self):
        res = assemble(Segmented(total_lines=0), SPECS)
        self.assertEqual(res.fill_rates, {"title": 0.0, "body": 0.0})


class TestDedupe(unittest.TestCase):
    def test_identical_key_drops_the_later_row(self):
        rows = [Row(values={"t": "x"}), Row(values={"t": "x"}), Row(values={"t": "y"})]
        res = dedupe(_wrap(rows, ["t"]), ["t"])
        self.assertEqual([r.get("t") for r in res.rows], ["x", "y"])

    def test_rows_with_an_empty_key_are_always_kept(self):
        # This is the old script's bug: two different users posted identical
        # text, dedupe on the text alone deleted one of them. A missing key
        # must never collapse distinct records.
        rows = [Row(values={"t": None}), Row(values={"t": None})]
        res = dedupe(_wrap(rows, ["t"]), ["t"])
        self.assertEqual(len(res.rows), 2)
        self.assertTrue(any("去重键为空" in n for n in res.notes))

    def test_two_different_users_same_text_survive_with_a_compound_key(self):
        rows = [Row(values={"u": "张三", "t": "好用"}),
                Row(values={"u": "李四", "t": "好用"})]
        res = dedupe(_wrap(rows, ["u", "t"]), ["u", "t"])
        self.assertEqual(len(res.rows), 2)

    def test_no_key_means_no_dedupe(self):
        rows = [Row(values={"t": "x"}), Row(values={"t": "x"})]
        res = dedupe(_wrap(rows, ["t"]), [])
        self.assertEqual(len(res.rows), 2)

    def test_dedupe_reports_what_it_dropped(self):
        rows = [Row(values={"t": "x"}), Row(values={"t": "x"})]
        res = dedupe(_wrap(rows, ["t"]), ["t"])
        self.assertTrue(any("丢弃 1 条重复" in n for n in res.notes))

    def test_dedupe_keeps_unparsed_and_books_balanced(self):
        rows = [Row(values={"t": "x"}), Row(values={"t": "x"})]
        base = _wrap(rows, ["t"], unparsed=["垃圾"], total=3, consumed=2)
        res = dedupe(base, ["t"])
        self.assertEqual(res.unparsed, ["垃圾"])
        self.assertIsNone(res.check_conservation())


class TestAssembleDedupeIntegration(unittest.TestCase):
    def test_card_dedupe_key_runs_automatically(self):
        seg = seg_of(["x", "1"], ["x", "2"], ["y", "3"])
        res = assemble(seg, SPECS, AssembleOptions(dedupe_by=["title"]))
        self.assertEqual([r.get("title") for r in res.rows], ["x", "y"])


class TestMergePages(unittest.TestCase):
    def test_pages_concatenate_and_keep_unparsed(self):
        a = assemble(seg_of(["a", "1"], unparsed=["p1垃圾"]), SPECS)
        b = assemble(seg_of(["b", "2"], unparsed=["p2垃圾"]), SPECS)
        merged = merge_pages([a, b])
        self.assertEqual(len(merged.rows), 2)
        self.assertEqual(merged.unparsed, ["p1垃圾", "p2垃圾"])
        # 2 record lines + 1 unparsed garbage line per page, two pages.
        self.assertEqual(merged.total_lines, 6)
        self.assertIsNone(merged.check_conservation())

    def test_duplicate_records_across_pages_collapse(self):
        # The second page overlaps the first: an extremely common real case
        # when pagination is not strictly disjoint.
        a = assemble(seg_of(["x", "1"], ["y", "2"]), SPECS)
        b = assemble(seg_of(["y", "2"], ["z", "3"]), SPECS)
        merged = merge_pages([a, b], dedupe_by=["title"])
        self.assertEqual([r.get("title") for r in merged.rows], ["x", "y", "z"])

    def test_empty_list_is_an_empty_result(self):
        merged = merge_pages([])
        self.assertEqual(merged.rows, [])


def _wrap(rows, fields, *, unparsed=None, total=None, consumed=None):
    from clip2table.models import ParseResult
    return ParseResult(
        rows=rows, fields=fields,
        unparsed=list(unparsed or []),
        total_lines=total if total is not None else len(rows),
        consumed_lines=consumed if consumed is not None else len(rows),
        level="card",
    )


if __name__ == "__main__":
    unittest.main()
