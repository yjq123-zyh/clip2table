"""flow contract tests: one commit entry, and a preview that cannot half-write.

The defect this module exists to prevent was not "a missing flag check" -- it
was that `-d` and `--dry-run` meant slightly different things depending on which
command you typed them after. So these tests are about the boundary, not the
happy path:

* `run -d dir` must actually commit, or refuse loudly. Accepting the flag and
  dropping the rows is the bug.
* `--dry-run` must leave no directory behind, for every command that writes.
* the same text twice must not publish two pages.
* "what this run added" and "what the directory holds" must be two numbers.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from clip2table.cli import main
from clip2table.dataset import dataset_from_card
from clip2table.flow import TOTAL_KEYS, Totals

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ECOM = FIXTURES / "sample-ecom.txt"
GOV = FIXTURES / "sample-gov.txt"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestTotals(unittest.TestCase):
    def test_fetched_and_added_are_separate_numbers(self):
        t = Totals()
        t.fetched_rows = 12
        t.added = 3
        self.assertNotEqual(t.fetched_rows, t.added)

    def test_dict_carries_every_documented_key(self):
        d = Totals().as_dict()
        self.assertEqual(set(d), set(TOTAL_KEYS))
        for key in ("changed", "identity_conflicts", "duplicates", "empty_keys"):
            self.assertIn(key, d)


class TestRunIntoDataset(unittest.TestCase):
    """`run -d` used to parse, print, and drop the rows on the floor."""

    def test_run_with_dataset_actually_creates_it(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ds"
            code, out, err = run_cli("run", "-p", "generic-item-block",
                                     "--text-file", str(ECOM), "-d", str(target))
            self.assertEqual(code, 0, err)
            self.assertTrue(target.is_dir(), "run -d 没有建目录")
            self.assertTrue((target / "captures").is_dir())
            self.assertIn("提交进 dataset 3 行", out)

    def test_run_without_a_card_is_refused_before_writing(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ds"
            code, _, err = run_cli("run", "--text-file", str(ECOM),
                                   "-d", str(target))
            self.assertEqual(code, 2)
            self.assertIn("需要同时给 -p 一张卡", err)
            self.assertFalse(target.exists(), "拒绝之后仍然建了目录")

    def test_same_request_twice_does_not_publish_two_observations(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ds"
            args = ("run", "-p", "generic-item-block", "--text-file", str(ECOM),
                    "-d", str(target), "--request-id", "same")
            self.assertEqual(run_cli(*args)[0], 0)
            code, _, err = run_cli(*args)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(list((target / "captures").iterdir())), 1)

    def test_report_separates_this_run_from_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ds"
            run_cli("run", "-p", "generic-item-block",
                    "--text-file", str(ECOM), "-d", str(target))
            code, out, _ = run_cli("run", "-p", "generic-item-block",
                                   "--text-file", str(GOV), "-d", str(target),
                                   "--json")
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["this_run_rows"], 3)
            self.assertEqual(data["dataset_rows"], 6)
            self.assertNotEqual(data["this_run_rows"], data["dataset_rows"])


class TestDryRunWritesNothing(unittest.TestCase):
    """A preview that creates a directory is a preview that changed the world."""

    def test_run_dry_run_with_dataset_leaves_no_directory(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ds"
            code, _, err = run_cli("run", "-p", "generic-item-block",
                                   "--text-file", str(ECOM),
                                   "-d", str(target), "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertFalse(target.exists(), "--dry-run 建了 dataset 目录")
            self.assertIn("跳过整个提交步骤", err)

    def test_dry_run_still_prints_the_parsed_rows(self):
        code, out, _ = run_cli("run", "-p", "generic-item-block",
                               "--text-file", str(ECOM), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("title=", out)


class TestDatasetPageText(unittest.TestCase):
    def test_page_with_text_finds_the_published_page(self):
        with tempfile.TemporaryDirectory() as d:
            from clip2table.cards.loader import resolve_card
            from clip2table.core import parse_capture
            from clip2table.models import Captured

            card = resolve_card("generic-item-block")
            ds = dataset_from_card(card, str(Path(d) / "ds"))
            raw = ECOM.read_text(encoding="utf-8")
            cap = Captured(kind="text", payload=raw, channel="file")
            res, _ = parse_capture(cap, card)
            ds.commit_page(1, raw, {"channel": "file"}, res)
            self.assertEqual(ds.page_with_text(raw), 1)
            self.assertIsNone(ds.page_with_text(raw + "\n多余一行"))
            self.assertIsNone(ds.page_with_text(""))


if __name__ == "__main__":
    unittest.main()
