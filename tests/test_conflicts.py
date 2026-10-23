"""Dedupe must answer a question, not just count a number.

Three outcomes look alike from the outside and are completely different in
fact:

  * the record was updated      -> keep the new value, that IS the data
  * the key matched two things  -> keeping the first DELETES the second
  * the key is missing          -> no identity, cannot be judged

Collapsing them into "冲突 N 行" leaves the user to guess which one happened,
and the guess that loses data is invisible: a row is simply absent, with a
clean report and a conserved row count on top of it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clip2table.dataset import CONFLICTS, Dataset
from clip2table.models import ParseResult, Row


def _res(fields: list[str], *vals: dict) -> ParseResult:
    return ParseResult(rows=[Row(values=v) for v in vals], fields=fields)


def _mk(tmp: str, fields: list[str], on_collision: str = "report_only") -> Dataset:
    return Dataset(tmp, fields=fields, keys=["id"],
                   on_collision=on_collision).open()


class TestVerdict(unittest.TestCase):
    def test_one_mutable_column_is_an_update_not_a_clash(self):
        """A price moving is the normal case. Naming it a clash buries real ones."""
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price"])
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "书", "price": "10"}))
            out = ds.commit_page(2, "p2", {}, _res(["id", "title", "price"],
                                                   {"id": "1", "title": "书", "price": "12"}))
            c = out.outcome.details[0]
            self.assertEqual(c.verdict, "change")
            self.assertEqual(c.changed_fields, ["price"])
            self.assertEqual(out.outcome.changed, 1)
            self.assertEqual(out.outcome.identity_conflicts, 0)

    def test_most_columns_differing_is_only_a_hint_not_a_verdict(self):
        """The ratio cannot decide identity, so it must not pretend to.

        Without a card saying which fields identify the record, "almost every
        column changed" is equally consistent with a wrong key and with a
        record that was rewritten. Calling it an identity clash deleted nothing
        here, but it also never surfaced the one thing the user can act on --
        so it becomes a hint and the row keeps its "change" verdict.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price", "author"])
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price", "author"],
                                             {"id": "1", "title": "甲", "price": "10",
                                              "author": "张三"}))
            out = ds.commit_page(2, "p2", {}, _res(["id", "title", "price", "author"],
                                                   {"id": "1", "title": "乙", "price": "99",
                                                    "author": "李四"}))
            c = out.outcome.details[0]
            self.assertEqual(c.verdict, "change")
            self.assertEqual(sorted(c.changed_fields), ["author", "price", "title"])
            self.assertIn("主键可能选错", c.hint)
            self.assertIn("仅提示", c.hint)
            self.assertEqual(out.outcome.identity_conflicts, 0)

    def test_a_declared_identity_field_changing_is_a_real_clash(self):
        """A card that names 标题 as part of the identity can be believed."""
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["id", "title", "price"], keys=["id"],
                         on_collision="report_only",
                         identity_fields=["title"]).open()
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "甲", "price": "10"}))
            out = ds.commit_page(2, "p2", {}, _res(["id", "title", "price"],
                                                   {"id": "1", "title": "乙", "price": "10"}))
            c = out.outcome.details[0]
            self.assertEqual(c.verdict, "identity_conflict")
            self.assertIn("身份字段", c.hint)
            self.assertEqual(out.outcome.identity_conflicts, 1)

    def test_a_declared_mutable_field_changing_stays_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["id", "title", "price"], keys=["id"],
                         on_collision="report_only",
                         identity_fields=["title"], mutable_fields=["price"]).open()
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "甲", "price": "10"}))
            out = ds.commit_page(2, "p2", {}, _res(["id", "title", "price"],
                                                   {"id": "1", "title": "甲", "price": "12"}))
            c = out.outcome.details[0]
            self.assertEqual(c.verdict, "change")
            self.assertEqual(c.hint, "")

    def test_two_changes_out_of_many_is_still_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "t", "p", "a", "b", "c"])
            row1 = {"id": "1", "t": "x", "p": "1", "a": "1", "b": "2", "c": "3"}
            row2 = dict(row1, p="9", a="8")
            ds.commit_page(1, "p1", {}, _res(list(row1), row1))
            out = ds.commit_page(2, "p2", {}, _res(list(row1), row2))
            self.assertEqual(out.outcome.details[0].verdict, "change")

    def test_summary_names_both_kinds_separately(self):
        from clip2table.dataset import AppendOutcome, Conflict
        out = AppendOutcome(added=1, conflicts=2, details=[
            Conflict(key=("1",), kept={}, rejected={}, page=1, verdict="change"),
            Conflict(key=("2",), kept={}, rejected={}, page=2,
                     verdict="identity_conflict"),
        ])
        text = out.summary("report_only")
        self.assertIn("内容变更 1 行", text)
        self.assertIn("身份冲突 1 行", text)
        self.assertIn("身份字段变了", text)


class TestConflictsArePersisted(unittest.TestCase):
    def test_a_clash_survives_the_terminal_scrolling_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price"])
            ds.commit_page(1, "p1", {"channel": "http"},
                           _res(["id", "title", "price"],
                                {"id": "1", "title": "书", "price": "10"}))
            ds.commit_page(2, "p2", {"channel": "http"},
                           _res(["id", "title", "price"],
                                {"id": "1", "title": "书", "price": "12"}))
            saved = json.loads((Path(tmp) / CONFLICTS).read_text("utf-8"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["key"], ["1"])
            self.assertEqual(saved[0]["verdict"], "change")
            self.assertEqual(saved[0]["changed_fields"], ["price"])
            self.assertEqual(saved[0]["kept"]["price"], "10")
            self.assertEqual(saved[0]["rejected"]["price"], "12")
            self.assertTrue(saved[0]["at"])
            # Readable after the process that found it is long gone.
            again = Dataset(tmp).open()
            self.assertEqual(len(again.read_conflicts()), 1)

    def test_bookkeeping_columns_stay_out_of_the_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price"])
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "书", "price": "10"}))
            ds.commit_page(2, "p2", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "书", "price": "12"}))
            entry = ds.read_conflicts()[0]
            for blob in (entry["kept"], entry["rejected"]):
                self.assertFalse([k for k in blob if k.startswith("_")],
                                 "_page/_traces are ours, not the page's")

    def test_no_collision_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title"])
            ds.commit_page(1, "p1", {}, _res(["id", "title"],
                                             {"id": "1", "title": "x"}))
            self.assertFalse((Path(tmp) / CONFLICTS).exists())
            self.assertEqual(ds.read_conflicts(), [])

    def test_duplicates_are_not_written_as_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title"])
            ds.commit_page(1, "p1", {}, _res(["id", "title"],
                                             {"id": "1", "title": "x"}))
            out = ds.commit_page(2, "p2", {}, _res(["id", "title"],
                                                   {"id": "1", "title": "x"}))
            self.assertEqual(out.outcome.duplicates, 1)
            self.assertFalse((Path(tmp) / CONFLICTS).exists())

    def test_a_corrupt_log_does_not_break_the_next_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / CONFLICTS).write_text("{not json", encoding="utf-8")
            ds = _mk(tmp, ["id", "title", "price"])
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "书", "price": "1"}))
            ds.commit_page(2, "p2", {}, _res(["id", "title", "price"],
                                             {"id": "1", "title": "书", "price": "2"}))
            self.assertEqual(len(ds.read_conflicts()), 1)


class TestKeepAllKeepsDistinctVersions(unittest.TestCase):
    def test_the_same_second_version_is_not_appended_twice(self):
        """keep_all means "hold every distinct version", not "append arrivals".

        A page that repeats an already-seen record used to add that version
        again on every page, so the table grew rows that were neither new
        records nor new information.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price"], on_collision="keep_all")
            v1 = {"id": "1", "title": "书", "price": "10"}
            v2 = {"id": "1", "title": "书", "price": "12"}
            ds.commit_page(1, "p1", {}, _res(["id", "title", "price"], v1))
            ds.commit_page(2, "p2", {}, _res(["id", "title", "price"], v2))
            out = ds.commit_page(3, "p3", {}, _res(["id", "title", "price"], v2))
            self.assertEqual(out.outcome.added, 0)
            self.assertEqual(out.outcome.duplicates, 1)
            self.assertEqual(len(ds.rows), 2)
            self.assertEqual([r["price"] for r in ds.rows], ["10", "12"])

    def test_a_third_distinct_version_is_still_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title", "price"], on_collision="keep_all")
            for i, price in enumerate(("10", "12", "14")):
                ds.commit_page(i + 1, f"p{i + 1}", {}, _res(
                    ["id", "title", "price"],
                    {"id": "1", "title": "书", "price": price}))
            self.assertEqual([r["price"] for r in ds.rows], ["10", "12", "14"])


class TestMissingKey(unittest.TestCase):
    def test_a_row_without_a_key_is_kept_and_flagged(self):
        """No identity is not a reason to drop the record."""
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, ["id", "title"])
            out = ds.commit_page(1, "p1", {}, _res(["id", "title"],
                                                   {"id": "", "title": "无主键"}))
            self.assertEqual(out.outcome.empty_keys, 1)
            self.assertEqual(out.outcome.added, 1)
            self.assertEqual(len(ds.rows), 1)
            self.assertIn("去重键不完整", out.outcome.summary("report_only"))
            self.assertIn("身份未确定", out.outcome.summary("report_only"))
            self.assertEqual(ds.rows[0].get("_identity"), "未确定")

    def test_a_partial_composite_key_is_not_an_identity(self):
        """公司 alone must not stand in for 公司+订单号.

        `any()` used to accept it, so several rows whose 订单号 was missing all
        collapsed into one bucket and were merged as if they were one record.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["公司", "订单号", "金额"],
                         keys=["公司", "订单号"]).open()
            out = ds.commit_page(1, "p1", {}, _res(
                ["公司", "订单号", "金额"],
                {"公司": "甲", "订单号": "", "金额": "1"},
                {"公司": "甲", "订单号": "", "金额": "2"}))
            self.assertEqual(out.outcome.added, 2)
            self.assertEqual(out.outcome.duplicates, 0)
            self.assertEqual(out.outcome.empty_keys, 2)
            for row in ds.rows:
                self.assertEqual(row.get("_identity"), "未确定")

    def test_a_complete_composite_key_still_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["公司", "订单号", "金额"],
                         keys=["公司", "订单号"]).open()
            ds.commit_page(1, "p1", {}, _res(
                ["公司", "订单号", "金额"], {"公司": "甲", "订单号": "A1", "金额": "1"}))
            out = ds.commit_page(2, "p2", {}, _res(
                ["公司", "订单号", "金额"], {"公司": "甲", "订单号": "A1", "金额": "1"}))
            self.assertEqual(out.outcome.duplicates, 1)
            self.assertEqual(out.outcome.added, 0)


if __name__ == "__main__":
    unittest.main()
