"""S2-04: the ledger views, and the four traps they exist to avoid.

Each test describes the business behavior it covers: ordering, identity,
scope and the distinction between parsing corrections and business changes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clip2table import ledger as L
from clip2table import revisions as R
from clip2table.cards.loader import resolve_card
from clip2table.core import parse_capture
from clip2table.models import Captured

HEAD = "订单号\t客户\t金额\t状态\t下单时间\n"


def as_cap(text: str) -> Captured:
    return Captured(kind="text", payload=text, channel="file", engine=None,
                    formats=["file"])


class Temp:
    """A dataset in the system temp dir, fed one page at a time."""

    def __init__(self, card_name: str = "scenes/order-ledger/order-ledger.toml"):
        self.dir = Path(tempfile.mkdtemp(prefix="c2t-led-"))
        self.root = self.dir / "ds"
        R.init(self.root)
        self.card = resolve_card(card_name)
        self.base = None

    def collect(self, rows: str, page: int, *, header: bool = True) -> str:
        text = (HEAD if header else "") + rows
        run = R.new_run(self.root, task=self.card.name, base_revision=self.base)
        pub = R.Publisher(self.root, self.card, run=run, base_revision=self.base)
        cap = as_cap(text)
        res, _ = parse_capture(cap, self.card)
        pub.page(page, cap, res)
        rev = pub.publish(reason=f"page {page}")
        self.base = rev.revision_id
        return rev.revision_id

    def policy(self, **kw) -> L.LedgerPolicy:
        base = dict(keys=["订单号"], mutable=["状态", "下单时间"], sort="observed")
        if "field" in kw:
            # The CLI flag is --sort-field, the dataclass field is sort_field;
            # the tests say `field=` because that is what a card's [ledger] uses.
            kw["sort_field"] = kw.pop("field")
        base.update(kw)
        return L.LedgerPolicy(**base)

    def ledger(self, revision, *, baseline="", **kw) -> L.Ledger:
        return L.build(self.root, revision, self.policy(**kw), baseline=baseline)

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Temp()

    def tearDown(self) -> None:
        self.t.cleanup()

    def current(self, led: L.Ledger) -> dict[str, dict[str, str]]:
        return {c.label: c.values for c in led.current}


class TestCurrentViewFollowsTheDeclaredSort(LedgerCase):
    def test_a05_a_late_old_page_does_not_rewind_the_status(self):
        """A05: the newest business state wins even when it is observed first.

        Page 1 says 已发货 with 下单时间 3-20; page 2 arrives later but its
        下单时间 is 3-10, i.e. it describes an *earlier* state of the order.
        Picking "the one I read last" would roll the ledger back to 待付款.
        """
        rev1 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-20 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-10 09:00\n", 2)
        led = self.t.ledger(rev2, sort="business_time", field="下单时间")
        self.assertEqual(self.current(led)["A1"]["状态"], "已发货")
        self.assertEqual(led.current[0].basis, "2026-03-20 09:00")
        # Both are still on record: losing is not deletion.
        self.assertEqual([v.values["状态"] for v in led.history["text:A1"]],
                         ["已发货", "待付款"])

    def test_observed_sort_would_have_picked_the_late_arrival(self):
        """The same data under `observed` -- which is why the default is loud."""
        self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-20 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-10 09:00\n", 2)
        led = self.t.ledger(rev2, sort="observed")
        self.assertEqual(self.current(led)["A1"]["状态"], "待付款")

    def test_a06_a_tie_is_reported_not_silently_broken(self):
        """A06: same 下单时间, different 状态 -> pending, candidate kept."""
        self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2, sort="business_time", field="下单时间")
        cur = led.current[0]
        self.assertTrue(cur.pending, "a tie has to be named, not resolved silently")
        self.assertIn("排序值相同", cur.pending[0])
        self.assertEqual(len(led.pending), 1)
        # The row says what actually differs, so it is actionable.
        self.assertEqual([c.line() for c in led.pending[0].changes],
                         ["状态：待付款 → 已发货"])


class TestIdentityChangesAreNotUpdates(LedgerCase):
    def test_a07_a_changed_identity_field_goes_to_pending(self):
        """A07: 客户 is declared identity; changing it is not a normal update."""
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t李四\t¥10.00\t待付款\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1, identity=["客户"])
        self.assertEqual([c.kind for c in led.changes], ["pending"])
        self.assertIn("客户", led.pending[0].why)

    def test_a_leading_zero_key_is_not_merged_with_its_numeric_twin(self):
        """A07: `text` typing keeps 0012 and 12 apart unless the task says int.

        Two orders whose numbers differ only by a leading zero are two orders.
        Declaring the key as `int` is a deliberate statement that they are the
        same one, and the ledger then treats the second as a later observation.
        """
        rev = self.t.collect(
            "0012\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n"
            "12\t李四\t¥20.00\t待付款\t2026-03-14 10:00\n", 1)
        as_text = self.t.ledger(rev)
        self.assertEqual(sorted(self.current(as_text)), ["0012", "12"])
        as_int = self.t.ledger(rev, key_types={"订单号": "int"})
        self.assertEqual(len(self.current(as_int)), 1)
        # Merged means two observations of one entity, not one row silently lost.
        self.assertEqual(len(as_int.history["int:12"]), 2)

    def test_a_row_without_a_key_is_reported_not_glued_to_a_neighbour(self):
        """A07: a missing key is evidence, not a row to be merged somewhere."""
        rev = self.t.collect(
            "A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n"
            "\t李四\t¥20.00\t待付款\t2026-03-14 10:00\n", 1)
        led = self.t.ledger(rev)
        self.assertEqual(list(self.current(led)), ["A1"])
        self.assertEqual(len(led.skipped), 1)
        self.assertIn("订单号", led.skipped[0]["why"])


class TestChangesNeedABaseline(LedgerCase):
    def test_a05_status_change_appears_with_before_and_after(self):
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1)
        self.assertEqual([c.kind for c in led.changes], ["updated"])
        self.assertEqual([c.line() for c in led.changes[0].changes],
                         ["状态：待付款 → 已发货"])

    def test_a_new_entity_is_listed_with_its_content(self):
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n"
                              "A2\t李四\t¥20.00\t待付款\t2026-03-14 10:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1)
        rows, _ = led.change_rows()
        new = [r for r in rows if r["类型"] == "new"]
        self.assertEqual([r["实体"] for r in new], ["A2"])
        self.assertIn("客户=李四", new[0]["之后"])

    def test_a12_an_unobserved_entity_is_not_a_deletion(self):
        """A12: page 2 covers one order; the other keeps its known state."""
        rev1 = self.t.collect(
            "A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n"
            "A2\t李四\t¥20.00\t待付款\t2026-03-14 10:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1)
        self.assertEqual(sorted(self.current(led)), ["A1", "A2"])
        self.assertNotIn("A2", [c.label for c in led.changes])
        self.assertTrue(any("不当作删除" in n for n in led.notes))
        self.assertEqual(sorted(s["page"] for s in led.scope), [1, 2])

    def test_a13_a_rule_change_is_not_called_a_business_change(self):
        """A13: two parses with different rules must not be compared as business."""
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        # Reparse the same capture with a card whose 金额 rule moved.
        changed = resolve_card("scenes/order-ledger/order-ledger.toml")
        for f in changed.fields:
            if f.name == "金额":
                f.col = 3
        from clip2table import replay
        rep = replay.reparse(self.t.root, rev1, card=changed)
        led = self.t.ledger(rep.revision.revision_id, baseline=rev1)
        self.assertEqual([c.kind for c in led.changes], ["rule_reparse"])
        self.assertIn("规则不同", led.changes[0].why)


class TestTheExplanationQuotesTheRuleThatDecided(LedgerCase):
    def test_a_business_time_change_says_which_time_it_compared(self):
        """Under `business_time` the reason is the business time, not the clock.

        "观察时间 23:48:28 → 23:48:29" is true for every row of every run and
        therefore explains nothing: it says when we looked, not why this value
        won. The reason must name the field the card declared.
        """
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-10 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-20 09:00\n", 2)
        # 下单时间 moves with the state here, so it has to be declared mutable
        # too -- otherwise the row is correctly kept as pending ("a field you
        # never said would change did change") and never reaches the branch
        # this test is about.
        led = self.t.ledger(rev2, baseline=rev1, sort="business_time",
                            field="下单时间", mutable=["状态", "下单时间"])
        self.assertEqual([c.kind for c in led.changes], ["updated"])
        self.assertIn("下单时间", led.changes[0].why)
        self.assertIn("2026-03-10 09:00", led.changes[0].why)
        self.assertIn("2026-03-20 09:00", led.changes[0].why)
        self.assertNotIn("观察时间", led.changes[0].why)

    def test_observed_order_still_says_it_was_the_observation_time(self):
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1, sort="observed")
        self.assertIn("观察时间", led.changes[0].why)


class TestViewsAreDerivedNotWritten(LedgerCase):
    def test_the_three_views_come_from_the_same_revision(self):
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n"
                              "A2\t李四\t¥20.00\t待付款\t2026-03-15 10:00\n", 2)
        led = self.t.ledger(rev2, baseline=rev1)
        cur, cur_f = led.current_rows()
        chg, chg_f = led.change_rows()
        pen, pen_f = led.pending_rows()
        # The scene card declares six fields; these samples carry five, so
        # `更新时间` comes through empty. That is on purpose: this file is about
        # which observation wins, and every case here sets `sort="observed"` or
        # `business_time` on `下单时间`, so an empty extra column must not
        # change the answer. Asserting the exact column list catches the day a
        # ledger view silently starts dropping or reordering fields.
        self.assertEqual(cur_f, ["订单号", "客户", "金额", "状态", "下单时间",
                                 "更新时间", "_pending"])
        self.assertEqual(len(cur), 2)
        self.assertEqual(chg_f, ["类型", "实体", "字段", "之前", "之后", "说明"])
        self.assertEqual({r["类型"] for r in chg}, {"updated", "new"})
        self.assertEqual(pen_f, ["实体", "原因", "字段"])
        self.assertEqual(pen, [])

    def test_history_keeps_every_observation_newest_first(self):
        self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        led = self.t.ledger(rev2)
        seen = [v.values["状态"] for v in led.history["text:A1"]]
        self.assertEqual(seen, ["已发货", "待付款"])
        self.assertEqual([v.page for v in led.history["text:A1"]], [2, 1])

    def test_the_same_revision_read_twice_gives_the_same_answer(self):
        """No view is cached into a file, so a rebuild must be identical."""
        rev1 = self.t.collect("A1\t张三\t¥10.00\t待付款\t2026-03-14 09:00\n", 1)
        rev2 = self.t.collect("A1\t张三\t¥10.00\t已发货\t2026-03-14 09:00\n", 2)
        a = self.t.ledger(rev2, baseline=rev1).as_dict()
        b = self.t.ledger(rev2, baseline=rev1).as_dict()
        self.assertEqual(a, b)


class TestPolicyIsValidated(unittest.TestCase):
    def test_a_sort_rule_must_name_a_field(self):
        with self.assertRaises(Exception):
            L.LedgerPolicy(keys=["k"], sort="business_time")

    def test_sort_and_tie_breaking_are_separate_concepts(self):
        p = L.LedgerPolicy(keys=["k"], mutable=["a", "b"])
        self.assertEqual(p.sort, "observed")
        with self.assertRaises(Exception):
            L.LedgerPolicy(keys=["k"], mutable=["a"], identity=["a"])

    def test_the_policy_id_follows_its_content(self):
        a = L.LedgerPolicy(keys=["订单号"], mutable=["状态"], sort="observed")
        b = L.LedgerPolicy(keys=["订单号"], mutable=["状态"], sort="observed")
        c = L.LedgerPolicy(keys=["订单号"], mutable=["金额"], sort="observed")
        self.assertEqual(a.policy_id, b.policy_id)
        self.assertNotEqual(a.policy_id, c.policy_id)


if __name__ == "__main__":
    unittest.main()
