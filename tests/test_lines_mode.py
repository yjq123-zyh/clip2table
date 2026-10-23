"""`mode = "lines"` exists because `single` silently ate a whole table.

The built-in table card declared `mode = "single"`, which glues every non-empty
line of the capture into ONE record. Pasting an 11-line Excel range produced
1 row whose `order_no` was the header text "订单号" and whose `created` was
"下单时间" -- two fields at 100% fill, zero unparsed lines, conservation
balanced. Every signal the report shows said "clean parse". The table was gone.

So the rule is pinned here: a table card must yield one row per source line,
and `skip_header` must consume the header row rather than lose it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from clip2table.cards.loader import load_card, resolve_card
from clip2table.cards.schema import SEGMENT_MODES, parse_card
from clip2table.core import parse_capture
from clip2table.errors import InputError
from clip2table.models import Captured

ROOT = Path(__file__).resolve().parents[1]
TSV = ROOT / "fixtures" / "sample-excel-range.txt"
HTML = ROOT / "fixtures" / "sample-table.html"


def _cap(payload: str, kind: str = "text") -> Captured:
    return Captured(kind=kind, payload=payload, channel="file")


class TestLinesModeIsAccepted(unittest.TestCase):
    def test_lines_is_a_declared_mode(self):
        self.assertIn("lines", SEGMENT_MODES)

    def test_card_may_declare_lines(self):
        card = parse_card({
            "schema": 1,
            "name": "demo",
            "segment": {"mode": "lines"},
            "fields": {"a": {"kind": "cell", "col": 0}},
        })
        self.assertEqual(card.segment_mode, "lines")

    def test_skip_header_needs_lines_mode(self):
        with self.assertRaises(InputError) as ctx:
            parse_card({
                "schema": 1,
                "name": "demo",
                "segment": {"mode": "anchor", "anchor": ".", "skip_header": True},
                "fields": {"a": {"kind": "line", "index": 0}},
            })
        self.assertIn("skip_header", str(ctx.exception))


class TestBuiltinTableCardKeepsEveryRow(unittest.TestCase):
    def test_header_is_consumed_and_every_data_line_is_a_row(self):
        card = resolve_card("admin-order-list")
        lines = [ln for ln in TSV.read_text(encoding="utf-8").splitlines() if ln.strip()]
        res, _ = parse_capture(_cap(TSV.read_text(encoding="utf-8")), card)
        with self.subTest(source_lines=len(lines)):
            self.assertEqual(len(res.rows), len(lines) - 1)
            # The header must be accounted for, not lost: total == data + header.
            self.assertEqual(res.total_lines, len(lines))
            self.assertIsNone(res.check_conservation())

    def test_header_does_not_become_a_row(self):
        card = resolve_card("admin-order-list")
        res, _ = parse_capture(_cap(TSV.read_text(encoding="utf-8")), card)
        first = res.rows[0].values
        self.assertEqual(first["order_no"], "SO-2026-0001")
        self.assertEqual(first["created"], "2026-03-01 09:12")
        self.assertEqual(first["amount"], 129.0)
        # A header seeded row would show up here as "订单号".
        for row in res.rows:
            self.assertNotEqual(row.values.get("order_no"), "订单号")

    def test_skip_header_false_keeps_the_header_as_a_row(self):
        card = load_card(resolve_card("admin-order-list").path)
        card.skip_header = False
        res, _ = parse_capture(_cap(TSV.read_text(encoding="utf-8")), card)
        self.assertEqual(res.rows[0].values["order_no"], "订单号")
        self.assertEqual(res.fill_rates["amount"], 10 / 11)

    def test_single_would_have_collapsed_it(self):
        """Document the failure this mode prevents, not just the fix."""
        card = load_card(resolve_card("admin-order-list").path)
        card.segment_mode = "single"
        res, _ = parse_capture(_cap(TSV.read_text(encoding="utf-8")), card)
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0].values["order_no"], "订单号")

    def test_html_table_rows_become_rows(self):
        card = resolve_card("admin-order-list")
        res, _ = parse_capture(_cap(HTML.read_text(encoding="utf-8"), kind="html"), card)
        self.assertGreaterEqual(len(res.rows), 3)

    def test_lines_mode_does_not_invent_an_anchor(self):
        """No anchor + mode=lines must not fall back to 'nothing matched'."""
        card = parse_card({
            "schema": 1,
            "name": "demo",
            "segment": {"mode": "lines"},
            "fields": {"a": {"kind": "line", "index": 0}},
        })
        res, _ = parse_capture(_cap("甲\n乙\n丙\n"), card)
        self.assertEqual([r.values["a"] for r in res.rows], ["甲", "乙", "丙"])
        self.assertEqual(res.unparsed, [])


class TestSkipHeaderAccounting(unittest.TestCase):
    def test_skipped_header_is_not_reported_as_unparsed(self):
        card = parse_card({
            "schema": 1,
            "name": "demo",
            "segment": {"mode": "lines", "skip_header": True},
            "fields": {"a": {"kind": "cell", "col": 0}},
        })
        res, _ = parse_capture(_cap("名称\t数量\n甲\t1\n乙\t2\n"), card)
        self.assertEqual([r.values["a"] for r in res.rows], ["甲", "乙"])
        self.assertEqual(res.unparsed, [])
        self.assertIsNone(res.check_conservation())

    def test_single_line_capture_with_skip_header_yields_nothing(self):
        card = parse_card({
            "schema": 1,
            "name": "demo",
            "segment": {"mode": "lines", "skip_header": True},
            "fields": {"a": {"kind": "cell", "col": 0}},
        })
        res, _ = parse_capture(_cap("只有表头\n"), card)
        self.assertEqual(res.rows, [])
        self.assertIsNone(res.check_conservation())


if __name__ == "__main__":
    unittest.main()
