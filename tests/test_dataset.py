"""Dataset contract tests: pages are the evidence, views are derived.

The property under test is not "rows get written" -- it is that the directory
can be killed, reopened, and rebuilt without changing its row count. A rebuild
that grows the table means the losers of a dedupe collision came back, which
makes "rebuild" indistinguishable from "append the same data again".
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clip2table.dataset import (DUP_COL, PAGE_COL, Dataset, dataset_from_card)
from clip2table.models import ParseResult, Row


def _res(*vals: dict) -> ParseResult:
    return ParseResult(rows=[Row(values=v) for v in vals], fields=["a", "b"])


def _mk(tmp: str, on_collision: str = "report_only") -> Dataset:
    return Dataset(tmp, fields=["a", "b"], keys=["a"],
                   on_collision=on_collision).open()


class TestPageCommit(unittest.TestCase):
    def test_commit_publishes_three_files_and_row_file_holds_all_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            out = ds.commit_page(1, "raw one", {"channel": "http"},
                                 _res({"a": "1", "b": "x"}, {"a": "2", "b": "y"}))
            self.assertTrue(out.published)
            self.assertEqual(out.outcome.added, 2)
            d = Path(tmp) / "pages" / "0001"
            for name in ("raw.txt", "meta.json", "rows.json"):
                self.assertTrue((d / name).is_file(), name)
            self.assertEqual((d / "raw.txt").read_text(encoding="utf-8"), "raw one")
            # rows.json is the page's own evidence: every row it produced,
            # including any that lost a collision later.
            self.assertEqual(len(json.loads((d / "rows.json").read_text("utf-8"))), 2)
            self.assertEqual(ds.committed_pages(), [1])

    def test_recommitting_a_page_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "raw one", {"channel": "http"},
                           _res({"a": "1", "b": "x"}))
            again = ds.commit_page(1, "DIFFERENT raw", {"channel": "http"},
                                   _res({"a": "9", "b": "z"}))
            self.assertFalse(again.published)      # idempotent, not a second add
            self.assertEqual([r["a"] for r in ds.rows], ["1"])
            # Evidence is immutable: the second call must not overwrite raw.txt.
            self.assertEqual(
                (Path(tmp) / "pages" / "0001" / "raw.txt").read_text("utf-8"),
                "raw one")

    def test_half_written_page_dir_is_not_counted_as_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            d = Path(tmp) / "pages" / "0002"
            d.mkdir(parents=True)
            (d / "raw.txt").write_text("orphan", encoding="utf-8")
            self.assertEqual(ds.committed_pages(), [])
            self.assertEqual(ds.resume_plan("http")["resume"], False)

    def test_commit_refuses_to_double_count_across_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "p1", {"channel": "http"}, _res({"a": "1", "b": "x"}))
            out = ds.commit_page(2, "p2", {"channel": "http"},
                                 _res({"a": "1", "b": "x"}, {"a": "2", "b": "y"}))
            self.assertEqual(out.outcome.added, 1)
            self.assertEqual(out.outcome.duplicates, 1)
            self.assertEqual(len(ds.rows), 2)

    def test_conflicting_content_is_a_finding_not_a_silent_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "p1", {"channel": "http"}, _res({"a": "1", "b": "旧"}))
            out = ds.commit_page(2, "p2", {"channel": "http"},
                                 _res({"a": "1", "b": "新"}))
            self.assertEqual(out.outcome.conflicts, 1)
            self.assertEqual(len(out.outcome.details), 1)
            self.assertEqual(out.outcome.details[0].kept["b"], "旧")
            self.assertEqual(out.outcome.details[0].rejected["b"], "新")
            self.assertEqual([r["b"] for r in ds.rows], ["旧"])

    def test_keep_all_retains_both_and_marks_the_loser(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp, on_collision="keep_all")
            ds.commit_page(1, "p1", {"channel": "http"}, _res({"a": "1", "b": "旧"}))
            ds.commit_page(2, "p2", {"channel": "http"}, _res({"a": "1", "b": "新"}))
            self.assertEqual([r["b"] for r in ds.rows], ["旧", "新"])
            self.assertIn(DUP_COL, ds.rows[1])


class TestRebuildIsNotAppend(unittest.TestCase):
    """The one bug worth a named class: a rebuild must not resurrect dupes."""

    def _two_pages(self, on_collision: str) -> tuple[Dataset, str]:
        tmp = tempfile.mkdtemp()
        ds = _mk(tmp, on_collision)
        ds.commit_page(1, "p1", {"channel": "http"},
                       _res({"a": "1", "b": "x"}, {"a": "2", "b": "y"}))
        ds.commit_page(2, "p2", {"channel": "http"},
                       _res({"a": "2", "b": "y"}, {"a": "3", "b": "z"}))
        return ds, tmp

    def test_rebuild_keeps_the_row_count_stable(self):
        for mode in ("report_only", "keep_all"):
            with self.subTest(mode=mode):
                ds, tmp = self._two_pages(mode)
                live = [r["a"] for r in ds.rows]
                n = ds.rebuild()
                self.assertEqual(n, len(live))
                self.assertEqual([r["a"] for r in ds.rows], live)

    def test_reopening_replays_the_merge(self):
        for mode in ("report_only", "keep_all"):
            with self.subTest(mode=mode):
                ds, tmp = self._two_pages(mode)
                live = [r["a"] for r in ds.rows]
                again = Dataset(tmp).open()
                self.assertEqual([r["a"] for r in again.rows], live)

    def test_rows_csv_is_derived_and_does_not_drive_the_table(self):
        ds, tmp = self._two_pages("report_only")
        n = len(ds.rows)
        # Corrupt the view. The pages still say what happened.
        (Path(tmp) / "rows.csv").write_text(
            "\ufeffa,b\n9,9\n9,9\n9,9\n", encoding="utf-8")
        again = Dataset(tmp).open()
        self.assertEqual(len(again.rows), n)

    def test_page_column_survives_rebuild(self):
        ds, tmp = self._two_pages("report_only")
        again = Dataset(tmp).open()
        self.assertEqual([r[PAGE_COL] for r in again.rows], [1, 1, 2])


class TestResumePlan(unittest.TestCase):
    def test_empty_dataset_starts_at_one_without_claiming_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = _mk(tmp).resume_plan("http")
            self.assertEqual(plan["next_page"], 1)
            self.assertFalse(plan["resume"])

    def test_restorable_channels_continue_by_arithmetic(self):
        for ch in ("http", "browser"):
            with self.subTest(channel=ch), tempfile.TemporaryDirectory() as tmp:
                ds = _mk(tmp)
                ds.commit_page(3, "p3", {"channel": ch}, _res({"a": "1", "b": "x"}))
                plan = ds.resume_plan(ch)
                self.assertTrue(plan["resume"])
                self.assertEqual(plan["next_page"], 4)

    def test_desktop_refuses_to_guess_where_the_window_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(2, "p2", {"channel": "desktop"},
                           _res({"a": "1", "b": "x"}))
            plan = ds.resume_plan("desktop")
            self.assertFalse(plan["resume"])
            self.assertTrue(plan["needs_explicit_append"])
            self.assertIn("--append-from-here", plan["why"])

    def test_append_from_here_tags_the_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "p1", {"channel": "desktop"}, _res({"a": "1", "b": "x"}))
            ds.commit_page(2, "p2", {"channel": "desktop"},
                           _res({"a": "2", "b": "y"}), append_from_here=True)
            rec = json.loads(
                (Path(tmp) / "pages" / "0002" / "rows.json").read_text("utf-8"))
            self.assertEqual(rec[0]["_segment"], "append@2")
            self.assertEqual(ds.state["appended"], [2])

    def test_run_log_records_which_url_was_actually_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "p1", {"channel": "browser",
                                     "requested_url": "https://a/x",
                                     "observed_url": "https://b/y",
                                     "url_is_proven": True},
                           _res({"a": "1", "b": "x"}))
            meta = ds.read_page(1)["meta"]
            self.assertEqual(meta["observed_url"], "https://b/y")
            self.assertEqual(meta["requested_url"], "https://a/x")
            self.assertEqual(ds.state["last_provenance"]["observed_url"],
                             "https://b/y")


class TestDatasetFromCard(unittest.TestCase):
    def test_identity_and_keys_come_from_the_card(self):
        class _Card:
            name = "demo"
            field_names = ["a", "b"]
            dedupe_key = ["a"]
            on_collision = "keep_first"

        with tempfile.TemporaryDirectory() as tmp:
            ds = dataset_from_card(_Card(), tmp)
            self.assertEqual(ds.fields, ["a", "b"])
            self.assertEqual(ds.keys, ["a"])
            self.assertEqual(ds.on_collision, "keep_first")


if __name__ == "__main__":
    unittest.main()
