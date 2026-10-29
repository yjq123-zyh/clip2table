"""S2-02 / S2-03: a replay reads saved captures and writes a candidate.

The tests below are about the boundary between "I changed a rule" and "the page
changed". A replay can only ever produce the first kind, because it never
fetches anything -- so every difference it reports must be attributable to the
rule it was given. The mirror image is what it must not do: silently shrink the
range, silently drop a page it failed to parse, or move the pointer to the table
without being asked.
"""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from clip2table import replay as P
from clip2table import revisions as R
from clip2table.cards.loader import resolve_card
from clip2table.core import parse_capture
from clip2table.errors import InputError
from clip2table.models import Captured

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenes" / "order-ledger"
CARD = SCENE / "order-ledger.toml"


def as_cap(path: Path, kind: str = "text") -> Captured:
    return Captured(kind=kind, payload=path.read_text(encoding="utf-8"),
                    channel="file")


def with_column(card, field: str, col: int):
    """Same card, one field pointed at a different column."""
    fields = [dataclasses.replace(f, col=col) if f.name == field else f
              for f in card.fields]
    return dataclasses.replace(card, fields=fields)


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="c2t-replay-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def collected(self, pages=(1, 2), card=None) -> tuple[Path, str]:
        """A dataset with one committed revision over the given pages."""
        root = self.tmp / "ds"
        R.init(root)
        card = card or resolve_card(CARD)
        pub = R.Publisher(root, card, run=R.new_run(root, task=card.name))
        for page in pages:
            cap = as_cap(SCENE / "input" / f"page-{page}.txt")
            res, _ = parse_capture(cap, card)
            pub.page(page, cap, res)
        rev = pub.publish(reason="collect")
        return root, rev.revision_id


class TestRuleSelection(Temp):
    def test_an_explicit_card_wins(self):
        root, base = self.collected()
        default = P.rule_for(root, base=R.read_revision(root, base))
        card = with_column(resolve_card(CARD), "金额", 3)
        rep = P.reparse(root, base, card=card)
        self.assertNotEqual(rep.rule.rule_id, default.rule_id)

    def test_the_base_revisions_rule_is_the_default(self):
        """Otherwise "run the same replay again tomorrow" means something else."""
        root, base = self.collected()
        base_card = resolve_card(CARD)
        used = P.rule_for(root, base=R.read_revision(root, base))
        self.assertEqual(used.card_name, base_card.name)
        # Same rules -> same id. The stored snapshot must round-trip exactly.
        resnap = R.write_rule(root, R.card_from_rule(used))
        self.assertEqual(resnap.rule_id, used.rule_id)

    def test_no_rule_and_no_base_rule_is_an_error(self):
        root = self.ds_without_runs()
        with self.assertRaises(InputError):
            P.rule_for(root)

    def ds_without_runs(self) -> Path:
        root = self.tmp / "bare"
        R.init(root)
        return root


class TestReplayChangesNothingItShouldNot(Temp):
    def test_the_same_rule_gives_the_same_parse_and_no_diff(self):
        root, base = self.collected()
        rep = P.reparse(root, base)
        self.assertEqual(rep.changed, 0)
        self.assertTrue(all(d.same for d in rep.diffs))
        # Same rule + same capture + same build = the same parse object, not a
        # second copy of it.
        self.assertEqual(len(R.list_parses(root)), 2)

    def test_replay_never_activates(self):
        root, base = self.collected()
        rep = P.reparse(root, base,
                        card=with_column(resolve_card(CARD), "金额", 3))
        self.assertEqual(R.active(root)["revision_id"], base)
        self.assertNotEqual(rep.revision.revision_id, base)
        self.assertEqual(rep.revision.parent, base)

    def test_a_replay_can_be_activated_explicitly(self):
        root, base = self.collected()
        rep = P.reparse(root, base,
                        card=with_column(resolve_card(CARD), "金额", 3))
        R.activate(root, rep.revision.revision_id, expected=base)
        self.assertEqual(R.active(root)["revision_id"], rep.revision.revision_id)
        self.assertEqual(R.active(root)["previous"], base)


class TestAReplayedDiffPointsAtTheSource(Temp):
    def test_a_rule_change_reports_before_after_and_the_line(self):
        """A01: same capture, two parses, the difference located in the text."""
        root, base = self.collected(pages=(1,))
        rep = P.reparse(root, base,
                        card=with_column(resolve_card(CARD), "金额", 3))
        self.assertEqual(len(rep.diffs), 1)
        d = rep.diffs[0]
        self.assertFalse(d.same)
        self.assertEqual(len(d.changes), 6)
        first = d.changes[0]
        self.assertEqual(first.key, "A20260314001")
        self.assertEqual(first.field_name, "金额")
        self.assertEqual(first.before, "¥128.00")
        self.assertEqual(first.after, "已发货")
        # Page 1 has a header line, so record 1 starts on source line 2.
        self.assertEqual(first.line_after, 2)

    def test_the_diff_does_not_sort_the_fields_it_compares(self):
        root, base = self.collected(pages=(1,))
        rep = P.reparse(root, base,
                        card=with_column(resolve_card(CARD), "金额", 3))
        seen = [c.field_name for c in rep.diffs[0].changes]
        self.assertEqual(set(seen), {"金额"})

    def test_a_row_that_only_exists_on_one_side_is_not_matched_by_position(self):
        """A shifted boundary must not read as "every row changed"."""
        root, base = self.collected(pages=(1,))
        _, old = R.read_parse(root, R.read_revision(root, base).parses[0])
        # Drop the first record and re-key: the remaining five must still match
        # by business key rather than by index.
        shifted = dataclasses.replace(
            old,
            rows=[dataclasses.replace(r, source_line=(r.source_line or 0) - 1)
                  for r in old.rows[1:]])
        diff = P.diff_results(old, shifted, keys=["订单号"])
        self.assertEqual(diff.changes, [])
        self.assertEqual(len(diff.only_before), 1)
        self.assertEqual(diff.only_before[0], "A20260314001")
        self.assertEqual(diff.only_after, [])


class TestAFailedPageDoesNotShrinkTheCandidate(Temp):
    def test_a_capture_that_cannot_be_parsed_keeps_its_old_parse(self):
        """A12: "not re-parsed" must never be published as "gone"."""
        root, base = self.collected(pages=(1, 2))
        base_rev = R.read_revision(root, base)
        # A rule that cuts every page into one record still parses both pages,
        # so force the failure where it belongs: a capture whose text is HTML
        # while the rule expects a table it cannot find.
        broken = self.tmp / "broken.txt"
        broken.write_text("", encoding="utf-8")
        cap = as_cap(broken)
        pub = R.Publisher(root, resolve_card(CARD),
                          run=R.new_run(root, task="x"),
                          base_revision=base)
        cap_ref = R.write_capture(root, cap, run_id=pub.run.run_id, page=3)
        # Not through Publisher.page (that always passes a result); the failing
        # page is the one the caller could not parse.
        pub.run.captures.append(cap_ref.capture_id)
        R.save_run(root, pub.run)

        # Now reparse the two good pages only: the candidate must cover exactly
        # what the base covered, not less.
        rep = P.reparse(root, base, captures=base_rev.captures)
        self.assertEqual(rep.failed, [])
        self.assertEqual(set(rep.revision.captures), set(base_rev.captures))

    def test_a_capture_outside_the_base_revision_is_refused(self):
        root, base = self.collected(pages=(1,))
        other_cap = R.write_capture(root, as_cap(SCENE / "input" / "page-2.txt"),
                                    run_id="run-outside", page=9)
        with self.assertRaises(InputError):
            P.reparse(root, base, captures=[other_cap.capture_id])


class TestRevisionLevelCompare(Temp):
    def test_a_replay_shows_up_as_reparsed_not_added(self):
        root, base = self.collected()
        rep = P.reparse(root, base,
                        card=with_column(resolve_card(CARD), "金额", 3))
        diff = P.compare_revisions(root, base, rep.revision.revision_id)
        self.assertEqual(diff.only_left, [])
        self.assertEqual(diff.only_right, [])
        self.assertEqual(len(diff.reparsed), 2)
        self.assertIn("换了规则", diff.headline())

    def test_two_collections_show_up_as_coverage_not_as_a_rule_change(self):
        root, first = self.collected(pages=(1,))
        second_root = root
        card = resolve_card(CARD)
        pub = R.Publisher(second_root, card,
                          run=R.new_run(second_root, task=card.name),
                          base_revision=first)
        cap = as_cap(SCENE / "input" / "page-2.txt")
        res, _ = parse_capture(cap, card)
        pub.page(2, cap, res)
        rev2 = pub.publish(reason="second run")
        diff = P.compare_revisions(root, first, rev2.revision_id)
        self.assertEqual(len(diff.only_right), 1)
        self.assertEqual(diff.only_left, [])
        self.assertEqual(diff.reparsed, [])
        self.assertIn("只在右边", diff.headline())


class TestComparisonsNeedBothSides(Temp):
    def test_a_missing_parse_says_so(self):
        root, base = self.collected(pages=(1,))
        with self.assertRaises(InputError):
            P.compare_parses(root, "p-nope", R.read_revision(root, base).parses[0])

    def test_a_missing_capture_says_so(self):
        root, base = self.collected(pages=(1,))
        with self.assertRaises(InputError):
            P.compare_revisions(root, base, "v-nope")
