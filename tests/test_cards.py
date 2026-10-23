"""Cards are data. These tests are the difference between "add a site by
adding a file" and "add a site by reading a traceback".

Every rejection must name the offending key, because the person who wrote the
card is not the person who wrote the validator.
"""
from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from clip2table.cards.loader import list_builtin, load_card, resolve_card
from clip2table.cards.schema import Card, parse_card
from clip2table.errors import InputError

MINIMAL = {
    "schema": 1,
    "name": "demo",
    "segment": {"mode": "anchor", "anchor": "^(?!\\s).{1,40}$",
                "anchor_next": "\\d"},
    "fields": {"title": {"kind": "line", "index": 0}},
}


def card_dict(**overrides) -> dict:
    import copy
    d = copy.deepcopy(MINIMAL)
    d.update(overrides)
    return d


class TestHappyPath(unittest.TestCase):
    def test_minimal_card_parses(self):
        card = parse_card(card_dict())
        self.assertIsInstance(card, Card)
        self.assertEqual(card.name, "demo")
        self.assertEqual(card.field_names, ["title"])

    def test_defaults_are_conservative(self):
        card = parse_card(card_dict())
        self.assertEqual(card.on_collision, "report_only")
        self.assertEqual(card.max_pages, 1)
        self.assertEqual(card.segment_mode, "anchor")
        self.assertEqual(card.dedupe_key, [])


class TestEveryRejectionNamesTheKey(unittest.TestCase):
    def assertRejects(self, data: dict, *needles: str) -> None:
        with self.assertRaises(InputError) as ctx:
            parse_card(data)
        for n in needles:
            self.assertIn(n, str(ctx.exception))

    def test_unknown_top_level_key(self):
        self.assertRejects(card_dict(anchro="typo"), "anchro")

    def test_missing_schema(self):
        d = card_dict()
        del d["schema"]
        self.assertRejects(d, "schema")

    def test_wrong_schema_version(self):
        self.assertRejects(card_dict(schema=2), "schema")

    def test_bad_name_shape(self):
        self.assertRejects(card_dict(name="Demo Card"), "name")
        self.assertRejects(card_dict(name="-lead"), "name")

    def test_unknown_channel(self):
        self.assertRejects(card_dict(channel="selenium"), "channel")

    def test_missing_segment_block(self):
        d = card_dict()
        del d["segment"]
        self.assertRejects(d, "segment")

    def test_anchor_mode_without_an_anchor(self):
        self.assertRejects(card_dict(segment={"mode": "anchor"}), "anchor")

    def test_invalid_anchor_regex(self):
        self.assertRejects(
            card_dict(segment={"mode": "anchor", "anchor": "([",
                               "anchor_next": "."}), "anchor", "正则")

    def test_unknown_segment_mode(self):
        self.assertRejects(card_dict(segment={"mode": "fuzzy"}), "mode")

    def test_no_fields(self):
        self.assertRejects(card_dict(fields={}), "fields")

    def test_unknown_field_kind(self):
        self.assertRejects(
            card_dict(fields={"t": {"kind": "regex", "pattern": "."}}),
            "kind", "regex")

    def test_unknown_field_type(self):
        self.assertRejects(
            card_dict(fields={"t": {"kind": "line", "index": 0, "type": "decimal"}}),
            "type")

    def test_search_without_pattern(self):
        self.assertRejects(card_dict(fields={"t": {"kind": "search"}}), "pattern")

    def test_invalid_field_pattern(self):
        self.assertRejects(
            card_dict(fields={"t": {"kind": "search", "pattern": "(["}}), "正则")

    def test_const_without_value(self):
        self.assertRejects(card_dict(fields={"t": {"kind": "const"}}), "value")

    def test_dedupe_key_referencing_an_unknown_field(self):
        # A typo here silently disables dedupe, which is worse than an error.
        self.assertRejects(card_dict(dedupe={"key": ["titel"]}), "titel")

    def test_identity_referencing_an_unknown_field(self):
        self.assertRejects(
            card_dict(dedupe={"key": ["title"], "identity": ["titel"]}), "titel")

    def test_mutable_referencing_an_unknown_field(self):
        self.assertRejects(
            card_dict(dedupe={"key": ["title"], "mutable": ["titel"]}), "titel")

    def test_a_field_cannot_be_both_identity_and_mutable(self):
        # Declaring one column as both makes the verdict depend on which branch
        # runs first, i.e. on nothing the user can see.
        self.assertRejects(
            card_dict(dedupe={"key": ["title"], "identity": ["title"],
                              "mutable": ["title"]}),
            "同时被声明")

    def test_identity_and_mutable_are_read_from_the_card(self):
        card = parse_card(card_dict(
            fields={"title": {"kind": "line"},
                    "amount": {"kind": "line"},
                    "note": {"kind": "line"}},
            dedupe={"key": ["title"], "identity": ["title", "amount"],
                    "mutable": ["note"]}), "x.toml")
        self.assertEqual(card.identity_fields, ["title", "amount"])
        self.assertEqual(card.mutable_fields, ["note"])

    def test_unknown_on_collision(self):
        self.assertRejects(
            card_dict(dedupe={"key": ["title"], "on_collision": "merge"}),
            "on_collision")

    def test_paging_click_without_a_selector(self):
        self.assertRejects(card_dict(paging={"style": "click"}), "next")

    def test_unknown_paging_style(self):
        self.assertRejects(card_dict(paging={"style": "scroll"}), "style")


class TestLoader(unittest.TestCase):
    def test_reads_a_toml_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.toml"
            p.write_text(
                'schema = 1\nname = "demo"\n\n[segment]\nmode = "anchor"\n'
                "anchor = '^(?!\\\\s).{1,40}$'\nanchor_next = '\\\\d'\n\n"
                "[fields.title]\nkind = \"line\"\nindex = 0\n",
                encoding="utf-8")
            card = load_card(p)
            self.assertEqual(card.name, "demo")

    def test_missing_file_says_so(self):
        with self.assertRaises(InputError) as ctx:
            load_card("nope.toml")
        self.assertIn("找不到卡", str(ctx.exception))

    def test_non_toml_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.yaml"
            p.write_text("name: demo", encoding="utf-8")
            with self.assertRaises(InputError) as ctx:
                load_card(p)
            self.assertIn("toml", str(ctx.exception).lower())

    def test_malformed_toml_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.toml"
            p.write_text("schema = \nname = demo", encoding="utf-8")
            with self.assertRaises(InputError) as ctx:
                load_card(p)
            self.assertIn("TOML", str(ctx.exception))

    def test_resolve_by_builtin_name(self):
        card = resolve_card("generic-item-block")
        self.assertEqual(card.name, "generic-item-block")

    def test_resolve_unknown_name_lists_the_way_out(self):
        with self.assertRaises(InputError) as ctx:
            resolve_card("no-such-card")
        self.assertIn("cards list", ctx.exception.hint)


class TestContradictionBetweenTwoLines(unittest.TestCase):
    """The card that is shipped must itself obey the two-line rule.

    Every builtin card is parsed and its anchor_next applied to a line that is
    a body line starting with a date. A loose prefix match would split one
    record into two, so this test fails the card, not just the pipeline.
    """

    def test_generic_item_block_rejects_a_body_line_starting_with_a_date(self):
        import re
        from clip2table.pipeline.segment import segment
        card = resolve_card("generic-item-block")
        lines = ["轻羽人体工学椅", "2026-03-11", "腰托到位",
                 "2026-03-12 补记：扶手略窄", "有用(56)"]
        seg = segment(lines, anchor=card.anchor, anchor_next=card.anchor_next)
        self.assertEqual(len(seg.records), 1)
        self.assertTrue(re.match(card.anchor_next, "2026-03-11").group(0))
        self.assertIsNone(re.match(card.anchor_next, "2026-03-12 补记：扶手略窄"))


class TestAllBuiltinCardsLoad(unittest.TestCase):
    def test_every_shipped_card_is_valid(self):
        cards = list_builtin()
        self.assertTrue(cards, "至少应有一张内置卡")
        for p in cards:
            with self.subTest(card=p.name):
                card = load_card(p)
                self.assertEqual(card.schema, 1)
                self.assertTrue(card.fields)

    def test_every_field_pattern_compiles(self):
        import re
        for p in list_builtin():
            card = load_card(p)
            for f in card.fields:
                if f.pattern:
                    with self.subTest(card=p.name, field=f.name):
                        re.compile(f.pattern)

    def test_every_card_declares_a_segment_block(self):
        # Read the allowed modes from the schema instead of restating them.
        # A copy of the list here is a second source of truth: adding a mode
        # then fails the test for a reason that has nothing to do with the card.
        from clip2table.cards.schema import SEGMENT_MODES
        for p in list_builtin():
            card = load_card(p)
            with self.subTest(card=p.name):
                self.assertIn(card.segment_mode, SEGMENT_MODES)


if __name__ == "__main__":
    unittest.main()
