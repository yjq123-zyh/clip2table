"""Two costs that used to grow with the square of the page count.

Dedupe asked "have I seen this screen before?" by re-reading and re-hashing
every committed page's raw.txt, on every commit. And the derived table
(rows.csv) was rewritten for every page even when that page added nothing.

Both are invisible in a 3-page test and painful in a 3000-page one, so they are
pinned here by counting the work, not by timing it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clip2table.dataset import Dataset
from clip2table.models import ParseResult, Row


def _res(*vals: dict) -> ParseResult:
    return ParseResult(rows=[Row(values=v) for v in vals], fields=["a"])


def _mk(tmp: str) -> Dataset:
    return Dataset(tmp, fields=["a"], keys=["a"]).open()


class TestDedupeDoesNotRereadPages(unittest.TestCase):
    def test_looking_for_a_known_page_reads_no_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            for page in range(1, 6):
                ds.commit_page(page, f"raw {page}", {"channel": "http"},
                               _res({"a": str(page)}))
            # Reopen: the hashes come from meta.json, which is the point.
            ds2 = _mk(tmp)
            reads = []
            real = Path.read_text

            def counting(self, *a, **kw):
                if self.name == "raw.txt":
                    reads.append(self)
                return real(self, *a, **kw)

            with mock.patch.object(Path, "read_text", counting):
                self.assertEqual(ds2.page_with_text("raw 3"), 3)
                self.assertIsNone(ds2.page_with_text("never seen"))
            self.assertEqual(reads, [], "查重不该再读页面的原文")

    def test_a_page_just_committed_is_found_without_touching_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "raw one", {"channel": "http"}, _res({"a": "1"}))
            reads = []
            real = Path.read_text

            def counting(self, *a, **kw):
                if self.name == "raw.txt":
                    reads.append(self)
                return real(self, *a, **kw)

            with mock.patch.object(Path, "read_text", counting):
                self.assertEqual(ds.page_with_text("raw one"), 1)
            self.assertEqual(reads, [], "刚提交的页应当在缓存里")


class TestDerivedViewIsNotRewrittenForNothing(unittest.TestCase):
    def test_a_page_of_pure_duplicates_does_not_rewrite_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "raw one", {"channel": "http"}, _res({"a": "1"}))
            before = (Path(tmp) / "rows.csv").read_bytes()
            writes = []
            real = Dataset._write_rows

            def counting(self):
                writes.append(1)
                return real(self)

            with mock.patch.object(Dataset, "_write_rows", counting):
                out = ds.commit_page(2, "different screen, same row",
                                     {"channel": "http"}, _res({"a": "1"}))
            self.assertTrue(out.published, "原文不同，这一页仍应发布")
            self.assertEqual(out.outcome.added, 0)
            self.assertEqual(writes, [], "一行没变，不该重写 rows.csv")
            self.assertEqual((Path(tmp) / "rows.csv").read_bytes(), before)
            # The position still moved: skipping the rewrite must not skip the
            # bookkeeping, or a resume would fetch this page again.
            self.assertEqual(ds.state["pages_done"], 2)

    def test_a_page_with_new_rows_does_rewrite_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "raw one", {"channel": "http"}, _res({"a": "1"}))
            writes = []
            real = Dataset._write_rows

            def counting(self):
                writes.append(1)
                return real(self)

            with mock.patch.object(Dataset, "_write_rows", counting):
                ds.commit_page(2, "raw two", {"channel": "http"}, _res({"a": "2"}))
            self.assertEqual(len(writes), 1)
            body = (Path(tmp) / "rows.csv").read_text(encoding="utf-8-sig")
            self.assertIn("2", body)

    def test_rebuild_always_rewrites_because_the_view_is_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = _mk(tmp)
            ds.commit_page(1, "raw one", {"channel": "http"}, _res({"a": "1"}))
            # Make the view stale on purpose: rebuild's whole job is to restore
            # rows.csv from the pages, so it must not trust the dirty flag.
            (Path(tmp) / "rows.csv").write_text("a\n", encoding="utf-8-sig")
            writes = []
            real = Dataset._write_rows

            def counting(self):
                writes.append(1)
                return real(self)

            with mock.patch.object(Dataset, "_write_rows", counting):
                ds.rebuild()
            self.assertEqual(len(writes), 1)
            body = (Path(tmp) / "rows.csv").read_text(encoding="utf-8-sig")
            self.assertIn("1", body)


if __name__ == "__main__":
    unittest.main()
