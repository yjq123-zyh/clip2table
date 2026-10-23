"""The pages are the truth; state.json and rows.csv are caches.

These tests exist because the failure they catch is invisible: rows come back
correctly and the resume position silently does not, so the next run re-fetches
a page that was already committed and files it under the next number.
"""
import json
import tempfile
import unittest
from pathlib import Path

from clip2table.dataset import Dataset, ParseResult
from clip2table.models import Row


def _res(fields, *rows):
    return ParseResult(fields=fields, rows=[Row(values=dict(r)) for r in rows])


class TestStateIsACache(unittest.TestCase):
    def _ds(self, tmp):
        ds = Dataset(tmp, fields=["id", "title"], keys=["id"])
        ds.open()
        ds.commit_page(1, "one", {"channel": "http"},
                       _res(["id", "title"], {"id": "1", "title": "a"}))
        ds.commit_page(2, "two", {"channel": "http"},
                       _res(["id", "title"], {"id": "2", "title": "b"}))
        return ds

    def test_pages_done_comes_from_the_pages_not_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._ds(tmp)
            st = Path(tmp) / "state.json"
            data = json.loads(st.read_text("utf-8"))
            data["pages_done"] = 1          # the crash window
            st.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

            again = Dataset(tmp).open()
            self.assertEqual(len(again.rows), 2)
            self.assertEqual(again.state["pages_done"], 2)
            self.assertEqual(again.resume_plan("http")["next_page"], 3)

    def test_missing_state_is_rebuilt_from_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._ds(tmp)
            (Path(tmp) / "state.json").unlink()
            again = Dataset(tmp).open()
            self.assertEqual(len(again.rows), 2)
            self.assertEqual(again.fields, ["id", "title"])
            self.assertEqual(again.keys, ["id"])
            self.assertEqual(again.state["pages_done"], 2)

    def test_corrupt_state_is_rebuilt_from_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._ds(tmp)
            (Path(tmp) / "state.json").write_text("{oops", "utf-8")
            again = Dataset(tmp).open()
            self.assertEqual(len(again.rows), 2)
            self.assertEqual(again.resume_plan("http")["next_page"], 3)

    def test_checkpoint_follows_the_last_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._ds(tmp)
            again = Dataset(tmp).open()
            self.assertEqual(again.state["checkpoint"]["page"], 2)


class TestConflictsRebuildFromPages(unittest.TestCase):
    def test_detail_survives_losing_the_aggregate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["id", "title", "price"], keys=["id"])
            ds.open()
            ds.commit_page(1, "p1", {"channel": "http"},
                           _res(["id", "title", "price"],
                                {"id": "1", "title": "书", "price": "10"}))
            ds.commit_page(2, "p2", {"channel": "http"},
                           _res(["id", "title", "price"],
                                {"id": "1", "title": "书", "price": "12"}))
            (Path(tmp) / "conflicts.json").unlink()
            again = Dataset(tmp).open()
            got = again.read_conflicts()
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["page"], 2)
            self.assertEqual(got[0]["changed_fields"], ["price"])

    def test_aggregate_file_is_a_derived_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = Dataset(tmp, fields=["id", "price"], keys=["id"])
            ds.open()
            ds.commit_page(1, "p1", {"channel": "http"},
                           _res(["id", "price"], {"id": "1", "price": "10"}))
            ds.commit_page(2, "p2", {"channel": "http"},
                           _res(["id", "price"], {"id": "1", "price": "12"}))
            cached = json.loads((Path(tmp) / "conflicts.json").read_text("utf-8"))
            self.assertEqual(cached, Dataset(tmp).open().read_conflicts())


if __name__ == "__main__":
    unittest.main()
