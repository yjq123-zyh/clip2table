"""A legacy rows.csv dataset must not lose its history on the first append.

The bug this guards is invisible in the report: the first append shows the old
rows in memory and writes them to rows.csv, but the page it publishes holds only
the new rows. The next open replays pages and ignores rows.csv, so the history
is gone while "累计 N 行" still looks plausible.
"""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from clip2table.dataset import BASELINE_PAGE, ROWS, Dataset, ParseResult
from clip2table.models import Row


def _res(fields, *rows):
    return ParseResult(fields=fields, rows=[Row(values=dict(r)) for r in rows])


def _legacy(tmp, n=3):
    p = Path(tmp)
    fields = ["id", "title"]
    with (p / ROWS).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + ["_page"])
        w.writeheader()
        for i in range(1, n + 1):
            w.writerow({"id": str(i), "title": f"旧{i}", "_page": 1})
    (p / "state.json").write_text(json.dumps({
        "fields": fields, "keys": ["id"], "on_collision": "report_only",
        "rows": n, "pages_done": 0,
    }, ensure_ascii=False), "utf-8")


class TestLegacyBaseline(unittest.TestCase):
    def test_history_survives_the_first_append_and_a_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 3)
            ds = Dataset(tmp).open()
            self.assertEqual(len(ds.rows), 3)
            ds.commit_page(1, "new one", {"channel": "http"},
                           _res(["id", "title"], {"id": "9", "title": "新9"}))
            ds.save()
            again = Dataset(tmp).open()
            self.assertEqual([r["id"] for r in again.rows], ["1", "2", "3", "9"])

    def test_baseline_records_that_it_has_no_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 2)
            ds = Dataset(tmp).open()
            ds.commit_page(1, "new", {"channel": "http"},
                           _res(["id", "title"], {"id": "9", "title": "新"}))
            d = Path(tmp) / "pages" / f"{BASELINE_PAGE:04d}"
            meta = json.loads((d / "meta.json").read_text("utf-8"))
            self.assertTrue(meta["baseline"])
            self.assertEqual(meta["baseline_source"], ROWS)
            self.assertFalse(meta["raw_available"])
            self.assertEqual((d / "raw.txt").read_text("utf-8"), "")

    def test_baseline_is_not_counted_as_a_fetched_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 2)
            ds = Dataset(tmp).open()
            ds.commit_page(1, "new", {"channel": "http"},
                           _res(["id", "title"], {"id": "9", "title": "新"}))
            ds.save()
            again = Dataset(tmp).open()
            self.assertEqual(again.state["pages_done"], 1)
            self.assertEqual(again.resume_plan("http")["next_page"], 2)

    def test_baseline_is_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 2)
            ds = Dataset(tmp).open()
            ds.commit_page(1, "a", {"channel": "http"},
                           _res(["id", "title"], {"id": "8", "title": "八"}))
            ds.save()
            ds.commit_page(2, "b", {"channel": "http"},
                           _res(["id", "title"], {"id": "9", "title": "九"}))
            ds.save()
            dirs = [d.name for d in (Path(tmp) / "pages").iterdir() if d.is_dir()]
            self.assertEqual(sorted(dirs), ["0000", "0001", "0002"])
            self.assertEqual(len(Dataset(tmp).open().rows), 4)

    def test_an_empty_legacy_file_gets_no_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 0)
            ds = Dataset(tmp).open()
            ds.commit_page(1, "x", {"channel": "http"},
                           _res(["id", "title"], {"id": "1", "title": "a"}))
            ds.save()
            self.assertFalse((Path(tmp) / "pages" / f"{BASELINE_PAGE:04d}").is_dir())

    def test_resume_plan_explains_a_baseline_only_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            _legacy(tmp, 3)
            ds = Dataset(tmp).open()
            plan = ds.resume_plan("http")
            self.assertEqual(plan["next_page"], 1)
            self.assertFalse(plan["resume"])
            self.assertIn("基线", plan["why"])


if __name__ == "__main__":
    unittest.main()
