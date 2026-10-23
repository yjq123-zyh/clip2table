"""A dataset must remember what its columns meant when each row was taken.

The dataset already refuses a card with a different NAME. That catches almost
nothing in practice: people edit the card they already have. `title` keeps its
heading while the rule behind it moves from "line 0" to "line 1", and every row
after that is a different kind of thing sitting in the same column, under the
same table heading, with a clean report.

Refusing is the default because the alternative is silent. `--allow-card-change`
exists for the honest case (the site really did restructure and the user has
decided the old rows are still worth keeping), and it leaves a record.
"""
from __future__ import annotations

import tempfile
import tomllib
import unittest

from clip2table.cards.schema import parse_card
from clip2table.dataset import describe_card_drift, dataset_from_card
from clip2table.errors import InputError

TEMPLATE = """
schema = 1
name = "shop"
channel = "http"

[fetch]
url = "http://x/{{page}}"

[segment]
mode = "lines"

[fields.title]
kind = "line"
index = {title_index}

[fields.price]
kind = "line"
index = {price_index}

[dedupe]
key = ["title"]
"""


def _card(title_index: int = 0, price_index: int = 1, name: str = "shop"):
    text = TEMPLATE.format(title_index=title_index, price_index=price_index)
    text = text.replace('name = "shop"', f'name = "{name}"')
    return parse_card(tomllib.loads(text), "card.toml")


class TestCardBinding(unittest.TestCase):
    def test_first_use_records_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = dataset_from_card(_card(), tmp)
            snap = ds.state.get("card_snapshot") or {}
            self.assertEqual(snap["name"], "shop")
            self.assertEqual([f["name"] for f in snap["fields"]], ["title", "price"])
            self.assertIn("line index=0", snap["fields"][0]["rule"])

    def test_the_same_card_twice_is_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            ds = dataset_from_card(_card(), tmp)       # must not raise
            self.assertEqual(ds.state.get("card_drift") or [], [])

    def test_a_moved_rule_is_refused_and_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            # Same card name, same field name, different meaning.
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(_card(title_index=1, price_index=2), tmp)
            msg = str(ctx.exception)
            self.assertIn("title", msg)
            # Names the attribute that moved, not just the field: "index" alone
            # would not say whether the line moved or its pattern changed.
            self.assertIn("title.index", msg)
            self.assertIn("0 → 1", msg)
            self.assertIn("price.index", msg)

    def test_the_refusal_says_how_to_proceed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(_card(title_index=1), tmp)
            hint = getattr(ctx.exception, "hint", "") or ""
            self.assertIn("--allow-card-change", hint)

    def test_allowing_the_change_keeps_a_record_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            ds = dataset_from_card(_card(title_index=1), tmp,
                                   allow_card_change=True)
            drift = ds.state.get("card_drift") or []
            self.assertEqual(len(drift), 1)
            self.assertTrue(drift[0]["changes"])
            self.assertIn("title", " ".join(drift[0]["changes"]))
            # The snapshot moves forward, so the NEXT run compares against the
            # card actually in use rather than re-reporting the same change.
            self.assertIn("line index=1",
                          (ds.state["card_snapshot"]["fields"][0]["rule"]))

    def test_a_changed_key_is_drift_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = TEMPLATE.format(title_index=0, price_index=1).replace(
                'key = ["title"]', 'key = ["title", "price"]')
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("dedupe.keys", str(ctx.exception))

    def test_changing_only_a_line_fields_pattern_is_drift(self):
        """The gap the old fingerprint had: `line` recorded index only.

        Adding a pattern to a `line` field changes what the column contains
        without moving it, so an index-only snapshot compared equal and the
        table quietly mixed two meanings under one heading.
        """
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = TEMPLATE.format(title_index=0, price_index=1).replace(
                'index = 0',
                'index = 0' + chr(10) + "pattern = '^(\\\\d+)'", 1)
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("title.pattern", str(ctx.exception))

    def test_changing_skip_header_is_drift(self):
        """Whether the first row is data changes every row's meaning."""
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = TEMPLATE.format(title_index=0, price_index=1).replace(
                'mode = "lines"',
                'mode = "lines"' + chr(10) + 'skip_header = true')
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("skip_header", str(ctx.exception))

    def test_changing_how_far_the_harvest_runs_is_drift(self):
        """max_pages decides which rows exist, not just how they are labelled."""
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = TEMPLATE.format(title_index=0, price_index=1).replace(
                'mode = "lines"',
                'mode = "lines"' + chr(10) + chr(10)
                + '[paging]' + chr(10) + 'max_pages = 5')
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("paging.max_pages", str(ctx.exception))

    def test_changing_a_harvest_option_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = TEMPLATE.format(title_index=0, price_index=1).replace(
                'mode = "lines"',
                'mode = "lines"' + chr(10) + chr(10)
                + '[harvest]' + chr(10) + 'select_all = false')
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("harvest.select_all", str(ctx.exception))


    def test_a_channel_change_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_from_card(_card(), tmp)
            text = (TEMPLATE.format(title_index=0, price_index=1)
                    .replace('channel = "http"', 'channel = "browser"'))
            moved = parse_card(tomllib.loads(text), "card.toml")
            with self.assertRaises(InputError) as ctx:
                dataset_from_card(moved, tmp)
            self.assertIn("通道", str(ctx.exception))


class TestDriftWords(unittest.TestCase):
    """The message is the whole feature; it has to name the actual difference."""

    def test_added_and_removed_fields_are_named(self):
        old = {"fields": [{"name": "a", "type": "text", "rule": "line index=0"}],
               "keys": ["a"]}
        new = {"fields": [{"name": "b", "type": "text", "rule": "line index=0"}],
               "keys": ["b"]}
        words = describe_card_drift(old, new)
        self.assertIn("新增字段 b", words)
        self.assertIn("删掉字段 a", words)

    def test_nothing_changed_means_no_words(self):
        snap = {"channel": "http", "segment_mode": "lines", "anchor": None,
                "anchor_next": None, "keys": ["a"],
                "fields": [{"name": "a", "type": "text", "rule": "line index=0"}]}
        self.assertEqual(describe_card_drift(snap, dict(snap)), [])

    def test_no_old_snapshot_means_no_drift(self):
        """A directory created before snapshots existed must still open."""
        self.assertEqual(describe_card_drift({}, {"channel": "http"}), [])


if __name__ == "__main__":
    unittest.main()
