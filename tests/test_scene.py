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
import io
import json
import os
import runpy
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip2table.cards.loader import load_card
from clip2table.cli import main
from clip2table.core import parse_capture
from clip2table.models import Captured
from clip2table import tasks

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
    def test_every_piece_s1_06_asks_for_is_on_disk(self):
        """目标字段、输入样本、独立答案、任务配置、输出样本、操作示例."""
        for p in (CARD, PAGE1, PAGE2, EXPECTED, SCENE / "README.md"):
            self.assertTrue(p.is_file(), f"场景包缺文件：{p}")

    def test_the_card_declares_its_own_purpose(self):
        card = load_card(CARD)
        self.assertEqual(card.title, "订单台账")
        self.assertTrue(card.task.get("source"), "卡里没写数据来源")
        self.assertTrue(card.task.get("expected"), "卡里没写一条正确记录长什么样")
        self.assertEqual(card.field_names,
                         ["订单号", "客户", "金额", "状态", "下单时间"])
        self.assertEqual(card.dedupe_key, ["订单号"])

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
            with patch("clip2table.cli.cmd_harvest", side_effect=ChannelError("目标窗口不存在")):
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

    def test_a_page_committed_twice_is_refused_and_does_not_grow_the_table(self):
        with tempfile.TemporaryDirectory() as d:
            card = self._card_copy(d)
            ds = Path(d) / "orders"
            code, _, _ = run_cli("task", "run", "-p", str(card),
                                 "--text-file", str(PAGE1), "-d", str(ds))
            self.assertEqual(code, 0)
            code, out, err = run_cli("task", "run", "-p", str(card),
                                     "--text-file", str(PAGE1), "-d", str(ds))
            self.assertNotEqual(code, 0, "同一页重复提交必须非零退出")
            self.assertIn("已经作为第 1 页提交过", out + err)

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
        """README 说差别只有 name 与 [task] 里那几行字，这里验它。"""
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
            self.assertEqual(gen["dedupe"], ship["dedupe"])
            self.assertEqual(gen["channel"], ship["channel"])

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


if __name__ == "__main__":
    unittest.main()
