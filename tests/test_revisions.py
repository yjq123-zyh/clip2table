"""S2-01: captures, rule snapshots, parses and revisions are separate objects.

The point of these tests is not the round-trip of a JSON file. It is that the
four things keep four identities: the same bytes observed twice are two
captures, the same capture parsed twice is two parses, a revision names exactly
one parse per capture, and a v1 directory is neither rewritten nor required to
understand any of it.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clip2table import revisions as R
from clip2table.cards.loader import resolve_card
from clip2table.core import parse_capture
from clip2table.dataset import dataset_from_card
from clip2table.errors import InputError
from clip2table.models import Captured

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenes" / "order-ledger"
CARD = SCENE / "order-ledger.toml"


def as_cap(path: Path, kind: str = "text") -> Captured:
    return Captured(kind=kind, payload=path.read_text(encoding="utf-8"),
                    channel="file")


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="c2t-rev-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def ds(self, name: str = "ds") -> Path:
        p = self.tmp / name
        R.init(p)
        return p


class TestLayoutIsDetected(Temp):
    def test_an_empty_directory_is_not_a_dataset(self):
        self.assertEqual(R.layout_of(self.tmp / "nope"), 0)
        (self.tmp / "empty").mkdir()
        self.assertEqual(R.layout_of(self.tmp / "empty"), 0)

    def test_a_v1_directory_is_recognised_as_v1(self):
        """Only once it has content: an untouched directory is not a dataset."""
        ds = self.tmp / "old"
        card = resolve_card(CARD)
        d = dataset_from_card(card, ds)
        self.assertEqual(R.layout_of(ds), 0)
        text = (SCENE / "input" / "page-1.txt").read_text(encoding="utf-8")
        res, _ = parse_capture(Captured(kind="text", payload=text), card)
        d.commit_page(1, text, {"channel": "file"}, res)
        self.assertEqual(R.layout_of(ds), 1)

    def test_a_new_directory_is_recognised_as_v2(self):
        self.assertEqual(R.layout_of(self.ds()), 2)

    def test_init_is_idempotent_and_keeps_the_id(self):
        ds = self.ds()
        first = R.dataset_meta(ds)["dataset_id"]
        R.init(ds)
        self.assertEqual(R.dataset_meta(ds)["dataset_id"], first)

    def test_a_newer_layout_is_refused_rather_than_misread(self):
        ds = self.ds()
        meta = R.dataset_meta(ds)
        meta["layout"] = 99
        R._dump(meta, ds / R.DATASET_JSON)
        with self.assertRaises(InputError) as cm:
            R.init(ds)
        self.assertIn("99", str(cm.exception))


class TestCaptureIdentity(Temp):
    def test_a_capture_round_trips_byte_for_byte(self):
        ds = self.ds()
        cap = as_cap(SCENE / "input" / "page-1.txt")
        ref = R.write_capture(ds, cap, run_id="run-a", page=1)
        _, back = R.read_capture(ds, ref.capture_id)
        self.assertEqual(back.payload, cap.payload)
        self.assertEqual(back.kind, "text")

    def test_the_same_bytes_in_a_new_run_are_a_new_capture(self):
        """Two observations are two facts, even when the text is identical.

        Merging them would make the second run's page count depend on the
        first run's content, so a re-collected list would report fewer pages
        than it actually fetched.
        """
        ds = self.ds()
        cap = as_cap(SCENE / "input" / "page-1.txt")
        a = R.write_capture(ds, cap, run_id="run-a", page=1)
        b = R.write_capture(ds, cap, run_id="run-b", page=1)
        self.assertNotEqual(a.capture_id, b.capture_id)
        self.assertEqual(a.payload_hash, b.payload_hash)

    def test_writing_the_same_page_of_the_same_run_is_idempotent(self):
        ds = self.ds()
        cap = as_cap(SCENE / "input" / "page-1.txt")
        a = R.write_capture(ds, cap, run_id="run-a", page=1)
        b = R.write_capture(ds, cap, run_id="run-a", page=1)
        self.assertEqual(a.capture_id, b.capture_id)
        self.assertEqual(len(R.list_captures(ds)), 1)

    def test_kind_and_formats_survive_the_round_trip(self):
        """v1 stored neither, which is why an old page cannot be re-parsed."""
        ds = self.ds()
        cap = Captured(kind="html", payload="<table><tr><td>a</td></tr></table>",
                       channel="browser", engine="E1", formats=["CF_HTML", "text"])
        ref = R.write_capture(ds, cap, run_id="run-a", page=3)
        _, back = R.read_capture(ds, ref.capture_id)
        self.assertEqual(back.kind, "html")
        self.assertEqual(back.formats, ["CF_HTML", "text"])
        self.assertEqual(back.engine, "E1")

    def test_a_json_capture_keeps_its_structure(self):
        ds = self.ds()
        cap = Captured(kind="json", payload="", channel="http",
                       json_payload={"data": [{"id": 1}, {"id": 2}]})
        ref = R.write_capture(ds, cap, run_id="run-a", page=1)
        _, back = R.read_capture(ds, ref.capture_id)
        self.assertEqual(back.kind, "json")
        self.assertEqual(back.json_payload, {"data": [{"id": 1}, {"id": 2}]})

    def test_a_missing_capture_says_so(self):
        with self.assertRaises(InputError):
            R.read_capture(self.ds(), "c-nothing")


class TestRuleSnapshot(Temp):
    def test_a_snapshot_can_be_reloaded_as_a_working_card(self):
        """If this fails, "the same rules later" was never true."""
        ds = self.ds()
        card = resolve_card(CARD)
        snap = R.write_rule(ds, card)
        back = R.card_from_rule(snap)
        self.assertEqual(back.name, card.name)
        self.assertEqual(back.field_names, card.field_names)
        self.assertEqual(back.segment_mode, card.segment_mode)
        self.assertEqual(back.dedupe_key, card.dedupe_key)

    def test_a_reloaded_snapshot_parses_the_same_way(self):
        ds = self.ds()
        card = resolve_card(CARD)
        snap = R.write_rule(ds, card)
        text = (SCENE / "input" / "page-1.txt").read_text(encoding="utf-8")
        a, _ = parse_capture(as_cap(SCENE / "input" / "page-1.txt"), card)
        b, _ = parse_capture(Captured(kind="text", payload=text),
                             R.card_from_rule(snap))
        self.assertEqual([r.values for r in a.rows], [r.values for r in b.rows])

    def test_the_same_rules_get_the_same_id(self):
        ds = self.ds()
        card = resolve_card(CARD)
        self.assertEqual(R.write_rule(ds, card).rule_id,
                         R.write_rule(ds, card).rule_id)
        self.assertEqual(len(list((ds / R.RULES).iterdir())), 1)

    def test_editing_a_field_changes_the_rule_id(self):
        ds = self.ds()
        card = resolve_card(CARD)
        before = R.write_rule(ds, card)
        fields = list(card.fields)
        fields[2] = type(fields[2])(**{**fields[2].__dict__, "col": 3})
        edited = type(card)(**{**card.__dict__, "fields": fields})
        self.assertNotEqual(before.rule_id, R.write_rule(ds, edited).rule_id)


class TestOneCaptureManyParses(Temp):
    def _capture(self, ds):
        cap = as_cap(SCENE / "input" / "page-1.txt")
        return cap, R.write_capture(ds, cap, run_id="run-a", page=1)

    def test_changing_a_rule_reparses_without_recollecting(self):
        """A01: the capture is the same object, the parse is a new one."""
        ds = self.ds()
        card = resolve_card(CARD)
        cap, cref = self._capture(ds)
        r1 = R.write_rule(ds, card)
        res1, _ = parse_capture(cap, card)
        p1 = R.write_parse(ds, cref, r1, res1)

        fields = list(card.fields)
        for i, f in enumerate(fields):
            if f.name == "金额":
                fields[i] = type(f)(**{**f.__dict__, "col": 3})
        edited = type(card)(**{**card.__dict__, "fields": fields})
        r2 = R.write_rule(ds, edited)
        res2, _ = parse_capture(cap, edited)
        p2 = R.write_parse(ds, cref, r2, res2)

        self.assertEqual(p1.capture_id, p2.capture_id)
        self.assertNotEqual(p1.parse_id, p2.parse_id)
        self.assertEqual(res1.rows[0].values["金额"], "¥128.00")
        self.assertEqual(res2.rows[0].values["金额"], "已发货")

    def test_two_revisions_over_the_same_capture_keep_both(self):
        ds = self.ds()
        card = resolve_card(CARD)
        cap, cref = self._capture(ds)
        rule = R.write_rule(ds, card)
        res, _ = parse_capture(cap, card)
        pref = R.write_parse(ds, cref, rule, res)

        a = R.write_revision(ds, [(cref.capture_id, pref.parse_id)],
                             reason="first")
        b = R.write_revision(ds, [(cref.capture_id, pref.parse_id)],
                             reason="second", parent=a.revision_id)
        self.assertNotEqual(a.revision_id, b.revision_id)
        self.assertEqual(b.parent, a.revision_id)
        self.assertEqual(len(R.list_revisions(ds)), 2)

    def test_publishing_the_same_parse_twice_is_idempotent(self):
        ds = self.ds()
        card = resolve_card(CARD)
        cap, cref = self._capture(ds)
        rule = R.write_rule(ds, card)
        res, _ = parse_capture(cap, card)
        first = R.write_parse(ds, cref, rule, res)
        again = R.write_parse(ds, cref, rule, res)
        self.assertEqual(first.parse_id, again.parse_id)
        self.assertEqual(len(R.list_parses(ds)), 1)

    def test_a_parse_remembers_enough_to_compare_boundaries(self):
        """Raw line positions are what a value-level diff cannot reconstruct."""
        ds = self.ds()
        card = resolve_card(CARD)
        cap, cref = self._capture(ds)
        res, _ = parse_capture(cap, card)
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        _, back = R.read_parse(ds, pref.parse_id)
        self.assertEqual([r.source_line for r in back.rows],
                         [r.source_line for r in res.rows])
        self.assertEqual([list(r.raw_at) for r in back.rows],
                         [list(r.raw_at) for r in res.rows])
        self.assertEqual([r.raw_lines for r in back.rows],
                         [r.raw_lines for r in res.rows])

    def test_column_order_survives_a_reload(self):
        ds = self.ds()
        card = resolve_card(CARD)
        cap, cref = self._capture(ds)
        res, _ = parse_capture(cap, card)
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        _, back = R.read_parse(ds, pref.parse_id)
        self.assertEqual(back.fields, res.fields)


class TestRevisionRefusesBadMaps(Temp):
    def _one(self, ds):
        card = resolve_card(CARD)
        cap = as_cap(SCENE / "input" / "page-1.txt")
        cref = R.write_capture(ds, cap, run_id="run-a", page=1)
        res, _ = parse_capture(cap, card)
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        return cref, pref

    def test_the_same_capture_twice_is_refused(self):
        ds = self.ds()
        cref, pref = self._one(ds)
        with self.assertRaises(InputError) as cm:
            R.write_revision(ds, [(cref.capture_id, pref.parse_id)] * 2)
        self.assertIn("两次", str(cm.exception))

    def test_a_missing_capture_is_refused(self):
        ds = self.ds()
        _, pref = self._one(ds)
        with self.assertRaises(InputError):
            R.write_revision(ds, [("c-nope", pref.parse_id)])

    def test_a_parse_from_another_capture_is_refused(self):
        """Pairing them would attach rows to text they never came from."""
        ds = self.ds()
        card = resolve_card(CARD)
        a = as_cap(SCENE / "input" / "page-1.txt")
        b = as_cap(SCENE / "input" / "page-2.txt")
        ca = R.write_capture(ds, a, run_id="run-a", page=1)
        cb = R.write_capture(ds, b, run_id="run-a", page=2)
        rule = R.write_rule(ds, card)
        res_a, _ = parse_capture(a, card)
        pa = R.write_parse(ds, ca, rule, res_a)
        with self.assertRaises(InputError) as cm:
            R.write_revision(ds, [(cb.capture_id, pa.parse_id)])
        self.assertIn("不是捕获", str(cm.exception))

    def test_an_unbalanced_parse_makes_an_incomplete_revision(self):
        ds = self.ds()
        card = resolve_card(CARD)
        cap = as_cap(SCENE / "input" / "page-1.txt")
        cref = R.write_capture(ds, cap, run_id="run-a", page=1)
        res, _ = parse_capture(cap, card)
        res.total_lines = res.consumed_lines + 10     # 10 lines unaccounted
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        self.assertFalse(pref.complete)
        rev = R.write_revision(ds, [(cref.capture_id, pref.parse_id)],
                               reason="partial")
        self.assertFalse(rev.complete)


class TestActivation(Temp):
    def _rev(self, ds):
        card = resolve_card(CARD)
        cap = as_cap(SCENE / "input" / "page-1.txt")
        cref = R.write_capture(ds, cap, run_id="run-a", page=1)
        res, _ = parse_capture(cap, card)
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        return R.write_revision(ds, [(cref.capture_id, pref.parse_id)],
                                reason="ok")

    def test_activation_records_the_previous_choice(self):
        ds = self.ds()
        a = self._rev(ds)
        b = self._rev(ds)
        R.activate(ds, a.revision_id, expected=None)
        out = R.activate(ds, b.revision_id, expected=a.revision_id)
        self.assertEqual(out["previous"], a.revision_id)
        self.assertEqual(R.active(ds)["revision_id"], b.revision_id)

    def test_a_stale_expectation_is_refused(self):
        """Someone else's choice is not ours to overwrite."""
        ds = self.ds()
        a = self._rev(ds)
        R.activate(ds, a.revision_id, expected=None)
        with self.assertRaises(InputError) as cm:
            R.activate(ds, a.revision_id, expected="v-somewhere-else")
        self.assertIn("不是你以为的", str(cm.exception))

    def test_an_incomplete_revision_cannot_become_the_table(self):
        ds = self.ds()
        card = resolve_card(CARD)
        cap = as_cap(SCENE / "input" / "page-1.txt")
        cref = R.write_capture(ds, cap, run_id="run-a", page=1)
        res, _ = parse_capture(cap, card)
        res.total_lines = res.consumed_lines + 3
        pref = R.write_parse(ds, cref, R.write_rule(ds, card), res)
        rev = R.write_revision(ds, [(cref.capture_id, pref.parse_id)])
        with self.assertRaises(InputError) as cm:
            R.activate(ds, rev.revision_id)
        self.assertIn("不完整", str(cm.exception))
        # ...but it can still be looked at.
        R.activate(ds, rev.revision_id, allow_incomplete=True)
        self.assertEqual(R.active(ds)["revision_id"], rev.revision_id)


class TestRunsAreSeparateFromCaptures(Temp):
    def test_a_run_records_what_it_did(self):
        ds = self.ds()
        run = R.new_run(ds, task="order-ledger", kind="collect")
        self.assertEqual(run.status, "running")
        run.status = "completed"
        run.captures = ["c-1", "c-2"]
        R.save_run(ds, run)
        back = R.read_run(ds, run.run_id)
        self.assertEqual(back.captures, ["c-1", "c-2"])
        self.assertEqual(back.status, "completed")

    def test_runs_are_listed_independently(self):
        ds = self.ds()
        a = R.new_run(ds, task="t", kind="collect")
        b = R.new_run(ds, task="t", kind="reparse")
        self.assertEqual({r.run_id for r in R.list_runs(ds)}, {a.run_id, b.run_id})
        self.assertEqual(len(R.list_captures(ds)), 0)


class TestLegacyPages(Temp):
    def _v1(self):
        ds = self.tmp / "v1"
        card = resolve_card(CARD)
        d = dataset_from_card(card, ds)
        for p in (1, 2):
            text = (SCENE / "input" / f"page-{p}.txt").read_text(encoding="utf-8")
            res, _ = parse_capture(Captured(kind="text", payload=text), card)
            d.commit_page(p, text, {"channel": "file", "why": "v1"}, res)
        return ds

    def test_a_page_without_a_kind_is_reported_as_not_reparseable(self):
        """Guessing text-vs-HTML would produce an empty parse that looks fine."""
        pages = R.legacy_pages(self._v1())
        self.assertEqual([p["page"] for p in pages], [1, 2])
        for p in pages:
            self.assertTrue(p["has_raw"])
            self.assertFalse(p["kind_known"])
            self.assertFalse(p["reparseable"])
            self.assertIn("文本还是 HTML", p["why_not"])

    def test_adoption_refuses_to_pick_a_parser_for_you(self):
        new = self.ds()
        with self.assertRaises(InputError) as cm:
            R.adopt_legacy_page(new, self._v1(), 1)
        self.assertIn("不能替你选", str(cm.exception))

    def test_adopting_keeps_the_rows_the_page_actually_had(self):
        new = self.ds()
        v1 = self._v1()
        cref, pref = R.adopt_legacy_page(new, v1, 1, assume_kind="text")
        self.assertEqual(cref.observed_at, "")
        self.assertEqual(pref.rows, 6)
        _, res = R.read_parse(new, pref.parse_id)
        self.assertEqual(res.fields[0], "订单号")
        self.assertEqual(res.rows[0].values["客户"], "张三")
        self.assertNotIn("_page", res.rows[0].values)

    def test_adoption_does_not_touch_the_old_directory(self):
        new = self.ds()
        v1 = self._v1()
        before = {p.name: p.stat().st_mtime_ns for p in (v1 / "pages").rglob("*") if p.is_file()}
        R.adopt_legacy_page(new, v1, 1, assume_kind="text")
        after = {p.name: p.stat().st_mtime_ns for p in (v1 / "pages").rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(R.layout_of(v1), 1)
        self.assertFalse((v1 / R.DATASET_JSON).exists())

    def test_adopted_pages_become_a_usable_revision(self):
        new = self.ds()
        v1 = self._v1()
        entries = []
        for page in R.legacy_pages(v1):
            cref, pref = R.adopt_legacy_page(new, v1, page["page"],
                                             assume_kind="text")
            entries.append((cref.capture_id, pref.parse_id))
        rev = R.write_revision(new, entries, reason="adopt v1")
        self.assertTrue(rev.complete)
        self.assertEqual(len(rev.entries), 2)
        R.activate(new, rev.revision_id, allow_incomplete=True)
        self.assertEqual(R.active(new)["revision_id"], rev.revision_id)


class TestPublisher(Temp):
    """One run, one rule, one revision -- and captures that stay independent."""

    def _run(self, root, card, run, base=None):
        pub = R.Publisher(root, card, run=run, base_revision=base)
        for page in (1, 2):
            text = (SCENE / "input" / f"page-{page}.txt").read_text(encoding="utf-8")
            cap = Captured(kind="text", payload=text, channel="file")
            res, _ = parse_capture(cap, card)
            pub.page(page, cap, res)
        return pub

    def test_a_run_publishes_one_revision_over_its_pages(self):
        root = self.ds()
        card = resolve_card(CARD)
        run = R.new_run(root, task=card.name)
        pub = self._run(root, card, run)
        rev = pub.publish(reason="first")
        self.assertEqual(len(rev.entries), 2)
        self.assertTrue(rev.complete)
        self.assertEqual(R.active(root)["revision_id"], rev.revision_id)
        self.assertEqual(R.read_run(root, run.run_id).status, "completed")

    def test_a_second_run_over_the_same_bytes_is_a_new_revision(self):
        """A08: same content twice is two observations, not one repeat."""
        root = self.ds()
        card = resolve_card(CARD)
        first = self._run(root, card, R.new_run(root, task=card.name))
        rev1 = first.publish(reason="first")
        second = self._run(root, card, R.new_run(root, task=card.name),
                           base=rev1.revision_id)
        rev2 = second.publish(reason="second")
        self.assertNotEqual(set(rev1.captures), set(rev2.captures))
        self.assertEqual(rev2.parent, rev1.revision_id)
        self.assertEqual(len(R.list_captures(root)), 4)
        self.assertEqual(R.active(root)["previous"], rev1.revision_id)

    def test_a_run_with_no_pages_publishes_nothing(self):
        root = self.ds()
        card = resolve_card(CARD)
        run = R.new_run(root, task=card.name)
        pub = R.Publisher(root, card, run=run)
        self.assertIsNone(pub.publish())
        self.assertEqual(R.list_revisions(root), [])
        self.assertEqual(R.read_run(root, run.run_id).status, "failed")

    def test_a_page_whose_parse_failed_still_keeps_its_text(self):
        """Losing the capture because the parse failed would lose the evidence."""
        root = self.ds()
        card = resolve_card(CARD)
        run = R.new_run(root, task=card.name)
        pub = R.Publisher(root, card, run=run)
        text = (SCENE / "input" / "page-1.txt").read_text(encoding="utf-8")
        cap = Captured(kind="text", payload=text, channel="file")
        cref, pref = pub.page(1, cap, None)
        self.assertIsNone(pref)
        self.assertEqual(len(R.list_captures(root)), 1)
        self.assertEqual(R.read_capture(root, cref.capture_id)[1].payload, text)
        self.assertEqual(pub.entries, [])

    def test_an_incomplete_revision_is_published_but_not_activated(self):
        """A03: a partial reparse can be inspected, never silently becomes truth."""
        root = self.ds()
        card = resolve_card(CARD)
        good = self._run(root, card, R.new_run(root, task=card.name))
        base = good.publish(reason="good")

        run = R.new_run(root, task=card.name, base_revision=base.revision_id)
        pub = R.Publisher(root, card, run=run, base_revision=base.revision_id)
        text = (SCENE / "input" / "page-2.txt").read_text(encoding="utf-8")
        cap = Captured(kind="text", payload=text, channel="file")
        res, _ = parse_capture(cap, card)
        res.total_lines = res.consumed_lines + 4     # four lines unaccounted
        pub.page(9, cap, res)
        rev = pub.publish(reason="partial")
        self.assertFalse(rev.complete)
        # The table still points at the last complete version.
        self.assertEqual(R.active(root)["revision_id"], base.revision_id)


if __name__ == "__main__":
    unittest.main()
