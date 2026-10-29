"""Exercise the order-ledger scenario from sample to repeated collection.

This suite walks the same path the package's README tells a user to walk, and
compares the result to the answer file that was written by hand. It is the only
test that exercises the whole chain at once:

    sample -> task card -> preview -> two pages -> export -> run it again

The reason it is a real test and not just a demo: the README claims "第一次两页
采集不需要手改 TOML" and "输出与 expected.csv 逐字节一致". Both of those are
falsifiable, and this file falsifies them on every run.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import runpy
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip2table.cards.loader import load_card
from clip2table.cli import main
from clip2table.core import parse_capture
from clip2table.models import Captured
from clip2table import revisions, tasks

SCENE = Path(__file__).resolve().parent.parent / "scenes" / "order-ledger"
CARD = SCENE / "order-ledger.toml"
PAGE1 = SCENE / "input" / "page-1.txt"
PAGE2 = SCENE / "input" / "page-2.txt"
EXPECTED = SCENE / "output" / "expected.csv"


def as_text(path: Path) -> Captured:
    return Captured(kind="text", payload=path.read_text(encoding="utf-8"),
                    channel="file", engine=None, formats=["file"])


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestThePackageIsComplete(unittest.TestCase):
    def test_the_answer_file_matches_a_fresh_run(self):
        """The shipped answers are what the tool produces today.

        `make-answers.py` is the independent implementation (it does not import
        clip2table), so this pins the answers to the samples: if a sample page
        is edited and the answers are not regenerated, this fails here rather
        than showing up as a mysterious mismatch three tests later.
        """
        proc = subprocess.run(
            [sys.executable, str(SCENE / "make-answers.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0,
                         f"答案文件与输入对不上：{proc.stdout}{proc.stderr}")

    def test_every_piece_s1_06_asks_for_is_on_disk(self):
        """目标字段、输入样本、独立答案、任务配置、输出样本、操作示例."""
        for p in (CARD, PAGE1, PAGE2, EXPECTED, SCENE / "README.md",
                  SCENE / "make-answers.py"):
            self.assertTrue(p.is_file(), f"场景包缺文件：{p}")

    def test_the_card_declares_its_own_purpose(self):
        card = load_card(CARD)
        self.assertEqual(card.title, "订单台账")
        self.assertTrue(card.task.get("source"), "卡里没写数据来源")
        self.assertTrue(card.task.get("expected"), "卡里没写一条正确记录长什么样")
        self.assertEqual(card.field_names,
                         ["订单号", "客户", "金额", "状态", "下单时间", "更新时间"])
        self.assertEqual(card.dedupe_key, ["订单号"])
        # `mutable` says which columns are expected to change between two
        # observations of one order. Nothing in a single sample can prove that,
        # so it is declared by hand -- and the ledger's A05/A06 handling depends
        # on it, which is exactly why it belongs in the shipped card.
        # 更新时间 is in it too: it is the business timestamp itself, so every
        # status change carries a new one, and leaving it out reported each
        # normal update as "这些字段没被声明会变".
        self.assertEqual(card.mutable_fields, ["状态", "更新时间"])
        self.assertEqual(card.ledger_sort, "business_time")
        self.assertEqual(card.ledger_sort_field, "更新时间")

    def test_the_samples_have_a_header_and_a_thousand_separator(self):
        self.assertIn("订单号\t客户", PAGE1.read_text(encoding="utf-8"))
        self.assertIn("¥1,204.50", PAGE2.read_text(encoding="utf-8"),
                      "第二页必须带千分位，否则测不到引号那件事")


class TestTheCardParsesBothPages(unittest.TestCase):
    def test_page_one_gives_six_orders_and_no_header_row(self):
        res, _ = parse_capture(as_text(PAGE1), load_card(CARD))
        self.assertEqual(len(res.rows), 6)
        self.assertEqual(res.rows[0].values["订单号"], "A20260314001")
        self.assertNotIn("订单号", [r.values["订单号"] for r in res.rows],
                         "表头又变成一条记录了")

    def test_page_two_keeps_the_amount_exactly_as_written(self):
        res, _ = parse_capture(as_text(PAGE2), load_card(CARD))
        self.assertEqual(len(res.rows), 5)
        self.assertEqual(res.rows[1].values["金额"], "¥1,204.50")

    def test_rows_can_point_back_at_the_line_they_came_from(self):
        res, _ = parse_capture(as_text(PAGE1), load_card(CARD))
        self.assertEqual([r.source_line for r in res.rows], [1, 2, 3, 4, 5, 6])


class TestTheWholePath(unittest.TestCase):
    def test_live_two_page_collection_export_and_saved_task_reuse(self):
        from http.server import ThreadingHTTPServer
        handler = runpy.run_path(str(SCENE / "serve.py"))["OrderHandler"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}):
                card = Path(d) / "live.toml"
                url = f"http://127.0.0.1:{server.server_port}/orders?page={{page}}"
                code, out, err = run_cli("task", "new", "--text-file", str(PAGE1),
                                         "--source", url, "--channel", "http", "--max-pages", "2",
                                         "--yes", "--out", str(card))
                self.assertEqual(code, 0, out + err)
                code, out, err = run_cli("task", "check", str(card), "--text-file", str(PAGE1),
                                         "--confirm", "--json")
                self.assertEqual(code, 0, out + err)
                self.assertTrue(json.loads(out)["confirmed"])
                original = card.read_bytes()
                for name in ("first", "repeat"):
                    ds = Path(d) / name
                    code, out, err = run_cli("task", "run", "-p", str(card), "-d", str(ds), "--yes", "--json")
                    self.assertEqual(code, 0, out + err)
                    self.assertEqual(json.loads(out)["task_stats"]["last_rows"], 11)
                    result = Path(d) / f"{name}.csv"
                    code, out, err = run_cli("export", "-d", str(ds), "-f", "csv", "-o", str(result))
                    self.assertEqual(code, 0, out + err)
                    self.assertEqual(result.read_bytes(), EXPECTED.read_bytes())
                self.assertEqual(card.read_bytes(), original)
                stats = tasks.summarise(tasks.load(tasks.stats_path(card)))
                self.assertEqual((stats["runs"], stats["clean_reruns"]), (2, 1))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_wizard_source_configuration_for_browser_and_desktop(self):
        with tempfile.TemporaryDirectory() as d:
            for channel, source, next_step in (("browser", "https://orders.example/list", "a.next"),
                                               ("desktop", "订单管理", "pagedown")):
                with self.subTest(channel=channel):
                    card = Path(d) / f"{channel}.toml"
                    code, out, err = run_cli("task", "new", "--text-file", str(PAGE1),
                                             "--source", source, "--channel", channel, "--next", next_step,
                                             "--max-pages", "2", "--yes", "--out", str(card))
                    self.assertEqual(code, 0, out + err)
                    saved = load_card(card)
                    self.assertEqual(saved.max_pages, 2)
                    if channel == "browser":
                        self.assertEqual(saved.target["url"], source)
                        self.assertEqual(saved.harvest["url_contains"], source)
                        self.assertEqual((saved.paging_style, saved.paging_next), ("click", "a.next"))
                    else:
                        self.assertEqual(saved.target, {"title": source, "next_key": next_step})
            code, out, err = run_cli("task", "new", "--text-file", str(PAGE1),
                                     "--source", "https://orders.example/list", "--channel", "http",
                                     "--max-pages", "2", "--yes", "--out", str(Path(d) / "bad.toml"))
            self.assertNotEqual(code, 0)
            self.assertFalse((Path(d) / "bad.toml").exists())

    def test_confirmation_tracks_first_setup_and_invalidates_after_rule_change(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "orders.toml"
            with patch.object(tasks, "_now", return_value="2026-10-26T10:00:00+08:00"):
                code, out, err = run_cli("task", "new", "--text-file", str(PAGE1), "--yes", "--out", str(card))
                self.assertEqual(code, 0, out + err)
            for minute in (2, 5):
                with patch.object(tasks, "_now", return_value=f"2026-10-26T10:0{minute}:00+08:00"):
                    code, out, err = run_cli("task", "check", str(card), "--text-file", str(PAGE1), "--confirm", "--json")
                    self.assertEqual(code, 0, out + err)
            self.assertEqual(tasks.summarise(tasks.load(tasks.stats_path(card)))["setup_seconds"], 120)
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(PAGE2), "--json")
            self.assertFalse(json.loads(out)["confirmed"])
            code, out, err = run_cli("task", "fix", str(card), "--field", "金额", "--type", "float",
                                     "--text-file", str(PAGE1), "--write")
            self.assertEqual(code, 0, out + err)
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(PAGE1), "--json")
            self.assertFalse(json.loads(out)["confirmed"])

    def test_converting_to_a_new_browser_source_replaces_old_navigation(self):
        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "old.toml"
            changed = Path(d) / "new.toml"
            code, out, err = run_cli("task", "new", "--text-file", str(PAGE1), "--channel", "browser",
                                     "--source", "https://old.example/list?page={page}",
                                     "--yes", "--out", str(original))
            self.assertEqual(code, 0, out + err)
            self.assertEqual(load_card(original).paging_url_template, "https://old.example/list?page={page}")
            code, out, err = run_cli("task", "new", "-p", str(original), "--text-file", str(PAGE1),
                                     "--source", "https://new.example/orders", "--max-pages", "1",
                                     "--yes", "--out", str(changed))
            self.assertEqual(code, 0, out + err)
            saved = load_card(changed)
            self.assertIsNone(saved.paging_url_template)
            self.assertEqual(saved.target["url"], "https://new.example/orders")
            self.assertEqual(saved.harvest["url_contains"], "https://new.example/orders")

    def test_quality_constraints_are_visible_and_block_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            card = self._card_copy(d)
            card.write_text(card.read_text(encoding="utf-8").replace(
                "[segment]", '[segment]\nrequired = ["金额"]'), encoding="utf-8")
            source = Path(d) / "empty-amount.txt"
            source.write_text(PAGE1.read_text(encoding="utf-8").replace("¥128.00", ""), encoding="utf-8")
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(source), "--json")
            data = json.loads(out)
            self.assertTrue(data["deviations"])
            self.assertLess(data["fill_rates"]["金额"], 1)
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(source), "--confirm")
            self.assertNotEqual(code, 0)
            self.assertFalse(tasks.load(tasks.stats_path(card)).get("confirmations"))

    def test_failed_run_is_recorded(self):
        from clip2table.errors import ChannelError
        with tempfile.TemporaryDirectory() as d:
            card = self._card_copy(d)
            with patch("clip2table.harvest.harvest_pages", side_effect=ChannelError("目标窗口不存在")):
                code, out, err = run_cli("task", "run", "-p", str(card), "-d", str(Path(d) / "out"))
            self.assertNotEqual(code, 0)
            runs = tasks.load(tasks.stats_path(card))["runs"]
            self.assertFalse(runs[0]["ok"])
            self.assertIn("目标窗口", runs[0]["note"])

    def _card_copy(self, d: str) -> Path:
        """A throwaway copy of the shipped card.

        `task run` writes its run record BESIDE the card, which is correct for a
        user's own task and wrong for a test: running against the shipped card
        would leave `order-ledger.stats.json` inside the repository. Every test
        that runs a task therefore works on a copy.
        """
        card = Path(d) / "orders.toml"
        card.write_bytes(CARD.read_bytes())
        return card

    def test_two_pages_export_byte_for_byte_like_the_answer_file(self):
        with tempfile.TemporaryDirectory() as d:
            card = self._card_copy(d)
            ds = Path(d) / "orders"
            for page in (PAGE1, PAGE2):
                code, out, err = run_cli("task", "run", "-p", str(card),
                                         "--text-file", str(page),
                                         "-d", str(ds))
                self.assertEqual(code, 0, out + err)
            self.assertIn("已提交页 2", out)

            got = Path(d) / "got.csv"
            code, out, err = run_cli("export", "-d", str(ds), "-f", "csv",
                                     "-o", str(got))
            self.assertEqual(code, 0, out + err)
            self.assertEqual(got.read_bytes(), EXPECTED.read_bytes(),
                             "导出结果与独立答案不一致")

    def test_a_request_retried_does_not_grow_the_table(self):
        with tempfile.TemporaryDirectory() as d:
            card = self._card_copy(d)
            ds = Path(d) / "orders"
            code, _, _ = run_cli("task", "run", "-p", str(card),
                                 "--text-file", str(PAGE1), "-d", str(ds), "--request-id", "page-one")
            self.assertEqual(code, 0)
            code, out, err = run_cli("task", "run", "-p", str(card),
                                     "--text-file", str(PAGE1), "-d", str(ds), "--request-id", "page-one")
            self.assertEqual(code, 0, out + err)
            from clip2table import revisions
            self.assertEqual(len(revisions.list_captures(ds)), 1)

    def test_the_task_remembers_that_it_ran_twice(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "orders.toml"
            card.write_bytes(CARD.read_bytes())
            ds = Path(d) / "out"
            for page in (PAGE1, PAGE2):
                run_cli("task", "run", "-p", str(card),
                        "--text-file", str(page), "-d", str(ds))
            code, out, err = run_cli("task", "show", str(card))
            self.assertEqual(code, 0, out + err)
            self.assertIn("第 2 次运行", out)
            # Per-run timing, worded as this run's own duration. The old line
            # said "首次耗时" and only the first run carried a number, so a task
            # whose first run read one page looked permanently fast.
            self.assertIn("这次", out)
            self.assertNotIn("首次耗时", out)
            # 记录必须写在卡旁边：写在数据集里的话，这里就读不到
            self.assertTrue((Path(d) / "orders.stats.json").is_file())
            self.assertFalse((ds / "task-stats.json").is_file(),
                             "任务记录不该落在数据集目录里")


class TestRunningTheTestsDoesNotDirtyTheRepository(unittest.TestCase):
    def test_the_scene_directory_holds_nothing_but_the_package(self):
        """A run record inside `scenes/` means some test ran against the shipped
        card. It is not a cosmetic issue: the file then ships in the sdist, and
        the next person's `task show` reports a run that happened on this
        machine."""
        strays = sorted(p.name for p in SCENE.iterdir()
                        if p.is_file() and p.suffix == ".json")
        self.assertEqual(strays, [], f"场景目录被跑出来的东西污染了：{strays}")


class TestTheWizardProducesTheSameCard(unittest.TestCase):
    def test_the_generated_card_matches_the_shipped_one_apart_from_the_prose(self):
        """What a sample cannot tell you is exactly what the shipped card adds.

        README 说「向导产物与自己写的卡只差 name 和 [task] 那几行字」。这句话
        需要一条限定：一条样本说不出「哪个字段会变」和「按哪个时间判断新旧」，
        所以 `[dedupe].mutable` 与 `[ledger]` 是人写的。这里把那两处显式列出来
        比对，其余逐键要求相同——差别一旦扩大，这条测试就会红。
        """
        import tomllib

        with tempfile.TemporaryDirectory() as d:
            out_card = Path(d) / "wizard.toml"
            code, out, err = run_cli("task", "new", "--text-file", str(PAGE1),
                                     "--yes", "--out", str(out_card))
            self.assertEqual(code, 0, out + err)
            gen = tomllib.loads(out_card.read_text(encoding="utf-8"))
            ship = tomllib.loads(CARD.read_text(encoding="utf-8"))

            self.assertEqual(gen["segment"], ship["segment"])
            self.assertEqual(gen["fields"], ship["fields"])
            self.assertEqual(gen["channel"], ship["channel"])
            self.assertEqual(gen["dedupe"]["key"], ship["dedupe"]["key"])
            self.assertEqual(gen["dedupe"]["on_collision"],
                             ship["dedupe"]["on_collision"])
            # Hand-written, and only these:
            self.assertNotIn("mutable", gen["dedupe"])
            self.assertEqual(ship["dedupe"]["mutable"], ["状态", "更新时间"])
            self.assertNotIn("ledger", gen)
            self.assertEqual(ship["ledger"]["sort"], "business_time")

    def test_the_generated_card_runs_without_editing_it(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "wizard.toml"
            code, out, err = run_cli("task", "new", "--text-file", str(PAGE1),
                                     "--yes", "--out", str(card))
            self.assertEqual(code, 0, out + err)
            ds = Path(d) / "out"
            for page in (PAGE1, PAGE2):
                code, out, err = run_cli("task", "run", "-p", str(card),
                                         "--text-file", str(page), "-d", str(ds))
                self.assertEqual(code, 0, out + err)
            got = Path(d) / "got.csv"
            code, out, err = run_cli("export", "-d", str(ds), "-f", "csv",
                                     "-o", str(got))
            self.assertEqual(code, 0, out + err)
            self.assertEqual(got.read_bytes(), EXPECTED.read_bytes())


class TestFixDoesNotTouchTheFileUnlessAsked(unittest.TestCase):
    """`task fix` without `--write` must leave the card byte-identical.

    A preview that quietly rewrites the file is worse than no preview: the user
    cannot tell "I looked at it" from "I already saved it".
    """

    def test_a_fix_without_write_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "c.toml"
            card.write_bytes(CARD.read_bytes())
            before = card.read_bytes()
            code, out, err = run_cli(
                "task", "fix", str(card), "--field", "金额", "--kind", "search",
                "--pattern", r"¥\s*(?P<v>[0-9,]+\.[0-9]{2})",
                "--text-file", str(PAGE2), "--rows", "2")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(card.read_bytes(), before, "没加 --write 却改了卡文件")
            self.assertIn("没有写回卡文件", out)

    def test_a_fix_with_write_changes_only_that_one_field_block(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "c.toml"
            card.write_bytes(CARD.read_bytes())
            before = card.read_text(encoding="utf-8")
            code, out, err = run_cli(
                "task", "fix", str(card), "--field", "金额", "--kind", "search",
                "--pattern", r"¥\s*(?P<v>[0-9,]+\.[0-9]{2})",
                "--text-file", str(PAGE2), "--write")
            self.assertEqual(code, 0, out + err)
            after = card.read_text(encoding="utf-8")
            self.assertIn('[fields.\'金额\']', after)
            self.assertIn("search", after)
            # 其他四个字段段与 [task]、[dedupe] 都不能被动过
            for keep in ("[task]", "purpose = ", "[dedupe]", "[fields.'客户']",
                         "[fields.'下单时间']", "channel = \"desktop\""):
                self.assertIn(keep, after, f"改写把 {keep} 弄丢了")
            self.assertNotEqual(before, after)

    def test_a_fix_keeps_the_line_endings_the_file_arrived_with(self):
        """The docstring promises "every other byte alone", so the EOL counts.

        `read_text`/`write_text` silently convert: an LF-only card came back
        fully CRLF after one `task fix --write`, which makes every line show up
        as changed in git for a one-line edit. The shipped cards are LF (see
        `.gitattributes`), so this was the normal path, not an edge case.
        """
        for crlf in (False, True):
            with self.subTest(crlf=crlf):
                with tempfile.TemporaryDirectory() as d:
                    card = Path(d) / "c.toml"
                    body = CARD.read_bytes()
                    if crlf:
                        body = body.replace(b"\n", b"\r\n")
                    card.write_bytes(body)
                    code, out, err = run_cli(
                        "task", "fix", str(card), "--field", "金额",
                        "--kind", "cell", "--col", "2", "--type", "float",
                        "--text-file", str(PAGE2), "--write")
                    self.assertEqual(code, 0, out + err)
                    after = card.read_bytes()
                    if crlf:
                        self.assertEqual(
                            after.replace(b"\r\n", b"").count(b"\n"), 0,
                            "CRLF 的卡被改成了混着 LF 的")
                    else:
                        self.assertNotIn(b"\r\n", after,
                                         "LF 的卡被改成了 CRLF")

    def test_a_fix_refuses_a_file_with_mixed_line_endings(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "c.toml"
            card.write_bytes(CARD.read_bytes().replace(b"[segment]",
                                                       b"[segment]\r\n"))
            code, out, err = run_cli(
                "task", "fix", str(card), "--field", "金额",
                "--kind", "cell", "--col", "2", "--type", "float",
                "--text-file", str(PAGE2), "--write")
            self.assertNotEqual(code, 0, "混着两种换行的卡必须拒绝改写")
            self.assertIn("两种换行", out + err)


class TestTheLedgerAnswersAreReproducible(unittest.TestCase):
    """S2-08: three observations of the same ledger, checked against answers.

    The answer files are produced by `scenes/order-ledger/make-answers.py`, which
    reads the three sample pages and applies the three rules the card states in
    words -- deliberately without importing clip2table. If the ledger and the
    answers are ever the same code, "they agree" stops meaning anything.

    Only business facts are compared. The tool also emits prose (`_pending` says
    *why* a row is pending, naming an observation timestamp that changes every
    run); the answer files keep the yes/no part and the test drops the rest.
    """

    def _collected(self, d: str) -> Path:
        card = Path(d) / "orders.toml"
        card.write_bytes(CARD.read_bytes())
        ds = Path(d) / "ledger"
        for page in (PAGE1, PAGE2, SCENE / "input" / "page-3.txt"):
            code, out, err = run_cli("task", "run", "-p", str(card),
                                     "--text-file", str(page), "-d", str(ds))
            self.assertEqual(code, 0, out + err)
        return ds

    def _csv(self, *argv: str) -> list[list[str]]:
        out_path = Path(self._tmp) / f"{len(list(Path(self._tmp).iterdir()))}.csv"
        code, out, err = run_cli(*argv, "-f", "csv", "-o", str(out_path))
        self.assertEqual(code, 0, out + err)
        with out_path.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.reader(fh))

    @staticmethod
    def _answer(name: str) -> list[list[str]]:
        with (SCENE / "output" / name).open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.reader(fh))

    def test_three_observations_match_the_answer_files(self):
        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            card = Path(d) / "orders.toml"
            # The answer file compares "two pages" against "three pages", so the
            # baseline is the two-page revision -- the second one made, not the
            # newest. Reading it as "the newest" made every row look new.
            base = revisions.list_revisions(ds)[-2].revision_id

            cur = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "current")
            want = self._answer("expected-current.csv")
            self.assertEqual([r[:6] for r in cur], [r[:6] for r in want],
                             "当前台账与独立答案不一致")
            # `_pending` is prose in the tool and a flag in the answer file.
            self.assertEqual([bool(r[6]) for r in cur[1:]],
                             [bool(r[6]) for r in want[1:]],
                             "哪些行待处理与答案不一致")

            chg = self._csv("ledger", "-d", str(ds), "-p", str(card),
                            "--view", "changes", "--baseline", base)
            want_chg = self._answer("expected-changes.csv")
            # Answer has no 说明 column: it would carry run timestamps.
            self.assertEqual([r[:5] for r in chg], [r[:5] for r in want_chg],
                             "变化表与独立答案不一致")

            pen = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "pending")
            want_pen = self._answer("expected-pending.csv")
            self.assertEqual([r[0] for r in pen], [r[0] for r in want_pen])
            self.assertEqual([r[-1] for r in pen], [r[-1] for r in want_pen],
                             "待处理行的字段与答案不一致")

    def test_an_older_version_arriving_late_does_not_undo_a_newer_one(self):
        """A05: page 3 carries A20260314001 with an *earlier* 更新时间.

        The ledger sorts by the declared business time, so the late-arriving
        older row must lose -- reading order is not business order.
        """
        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            card = Path(d) / "orders.toml"
            rows = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "current")
            by_key = {r[0]: r for r in rows[1:]}
            self.assertEqual(by_key["A20260314001"][3], "已发货",
                             "晚到的旧版本把当前状态改回去了")
            self.assertEqual(by_key["A20260314002"][3], "已付款")
            self.assertEqual(by_key["A20260314005"][3], "已退款")
            # The history keeps both observations: nothing was overwritten.
            hist = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "history")
            states = [r for r in hist[1:] if r and r[0] == "A20260314001"]
            self.assertGreaterEqual(len({r[-2] for r in states}), 1)

    def test_a_tie_is_flagged_rather_than_silently_decided(self):
        """A06: two observations, same 更新时间, different 状态.

        A20260314004 was 已发货 on page 1 and 已完成 on page 3, both stamped
        2026-03-14 11:20. The ledger must not pretend this is a clean update.
        """
        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            card = Path(d) / "orders.toml"
            rows = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "current")
            tie = {r[0]: r for r in rows[1:]}["A20260314004"]
            self.assertIn("排序值相同", tie[6], "同一业务时间的两个版本没有被标出来")
            self.assertIn("内容不同", tie[6])
            # And it is not reported as an ordinary update either.
            base = revisions.list_revisions(ds)[-2].revision_id
            chg = self._csv("ledger", "-d", str(ds), "-p", str(card),
                            "--view", "changes", "--baseline", base)
            self.assertNotIn(("A20260314004", "状态"),
                             {(r[1], r[2]) for r in chg[1:] if r[1] == "A20260314004"
                              and r[2] == "状态" and r[0] == "updated"},
                             "同一业务时间的歧义被当成了一次普通更新")

    def test_an_order_that_was_not_observed_again_is_not_deleted(self):
        """A12: page 3 mentions six of twelve orders. The other six stay."""
        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            card = Path(d) / "orders.toml"
            rows = self._csv("ledger", "-d", str(ds), "-p", str(card), "--view", "current")
            keys = {r[0] for r in rows[1:]}
            self.assertEqual(len(keys), 12, "没被重新观察到的订单被当成删除了")
            self.assertIn("A20260315005", keys)

    def test_a_changed_rule_is_not_reported_as_a_business_change(self):
        """A13: reparse with a different rule, then compare the two revisions.

        The values differ, but not because the business changed -- so the diff
        has to say `rule_reparse`, not "状态 变了".
        """
        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            base = revisions.list_revisions(ds)[-1].revision_id
            card = Path(d) / "orders.toml"
            edited = Path(d) / "edited.toml"
            # Point 状态 at the 下单时间 column: same parser, different rule, so
            # every value changes while no order did.
            edited.write_text(
                card.read_text(encoding="utf-8").replace(
                    'col  = 3', 'col  = 4', 1),
                encoding="utf-8")
            code, out, err = run_cli("replay", "-d", str(ds), "-p", str(edited),
                                     "--show", "3", "--select")
            self.assertEqual(code, 0, out + err)
            self.assertIn("处字段变化", out)
            new_rev = revisions.list_revisions(ds)[-1].revision_id
            chg = self._csv("ledger", "-d", str(ds), "-p", str(card),
                            "--view", "changes", "--baseline", base)
            kinds = {(r[0], r[2]) for r in chg[1:]}
            self.assertIn(("rule_reparse", "状态"), kinds,
                          "换规则引起的变化被混成了业务变化")
            self.assertEqual(new_rev, revisions.list_revisions(ds)[-1].revision_id)


    def test_the_three_views_export_to_xlsx(self):
        """S2-08 asks for 当前台账 / 变化表 / 待处理表 as files, not screens.

        The renderer is the same one the single-table export uses, so what is
        checked here is that all three views reach it with their own columns --
        a view that exports the wrong header would look fine on screen and be
        useless in the spreadsheet the user actually opens.
        """
        import zipfile

        with tempfile.TemporaryDirectory() as d:
            self._tmp = d
            ds = self._collected(d)
            card = Path(d) / "orders.toml"
            base = revisions.list_revisions(ds)[-2].revision_id
            expect = {
                "current": ["订单号", "_pending"],
                "changes": ["类型", "实体", "字段"],
                "pending": ["实体", "原因"],
            }
            for view, must in expect.items():
                with self.subTest(view=view):
                    out_path = Path(d) / f"{view}.xlsx"
                    argv = ["ledger", "-d", str(ds), "-p", str(card),
                            "--view", view, "-f", "xlsx", "-o", str(out_path)]
                    if view == "changes":
                        argv += ["--baseline", base]
                    code, out, err = run_cli(*argv)
                    self.assertEqual(code, 0, out + err)
                    self.assertTrue(out_path.is_file())
                    xml = zipfile.ZipFile(out_path).read(
                        "xl/worksheets/sheet1.xml").decode("utf-8")
                    head = re.findall(r"<t[^>]*>([^<]*)</t>", xml)
                    for col in must:
                        self.assertIn(col, head[:8],
                                      f"{view} 导出的表头没有 {col}")


if __name__ == "__main__":
    unittest.main()
