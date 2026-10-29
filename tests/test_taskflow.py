"""tests for the task layer: provenance-to-preview, the wizard, and the fixer.

User workflows covered here:
  - a field's value can be traced to a real line of the pasted text
  - a card built from a sample reproduces the sample without hand-editing TOML
  - one fix applies to the whole batch and can be saved
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from clip2table import taskflow, tasks
from clip2table.cards.schema import parse_card
from clip2table.core import parse_capture
from clip2table.models import Captured
from clip2table.preview import field_sources, grid, numbered_source

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def _cap(name: str) -> Captured:
    txt = (FIX / name).read_text(encoding="utf-8")
    return Captured(kind="text", payload=txt, channel="file", engine=None,
                    formats=["file"])


class TestProvenance(unittest.TestCase):
    """预览中的字段能够定位原文."""

    def test_saved_table_rules_keep_html_navigation_out_of_records(self):
        cap = Captured(kind="html", channel="file", payload=(
            "<head><title>订单</title></head><div>菜单\n说明</div>"
            "<table><tr><th>id</th><th>name</th></tr>"
            "<tr><td>1</td><td>甲<br>乙</td></tr></table><a>下一页</a>"))
        card = parse_card({"schema": 1, "name": "html", "segment": {"mode": "lines", "skip_header": True},
                           "fields": {"id": {"kind": "cell", "col": 0}, "name": {"kind": "cell", "col": 1}}})
        res = parse_capture(cap, card)[0]
        self.assertEqual([r.values for r in res.rows], [{"id": "1", "name": "甲 乙"}])
        self.assertEqual(res.rows[0].raw_at, [3])
        self.assertEqual(res.unparsed, ["菜单", "说明", "下一页"])
        self.assertIsNone(res.check_conservation())

    def test_infer_rows_carry_a_source_line(self):
        res, _inf = parse_capture(_cap("sample-ecom.txt"), None)
        self.assertTrue(all(r.source_line is not None for r in res.rows),
                        "推断出来的记录也必须知道自己在原文的第几行")
        # Records start on 1, 7, 13 (0-based 0, 6, 12) in the 15-line fixture.
        self.assertEqual([r.source_line for r in res.rows], [0, 6, 12])

    def test_both_specs_point_at_a_real_line_number(self):
        res, _inf = parse_capture(_cap("sample-ecom.txt"), None)
        line = field_sources(res, "日期", limit=1)[0]
        # "第 2 行" is the human-visible number: 0-based 0 start + trace 1 + 1.
        self.assertIn("第 2 行", line)
        self.assertIn("2026-03-14", line)

    def test_a_line_rule_trace_points_inside_the_record(self):
        card = _card_from("sample-ecom.txt")
        res, _inf = parse_capture(_cap("sample-ecom.txt"), card)
        r = res.rows[0]
        t = r.traces["日期"]
        self.assertEqual(t.line, 1, "日期 是记录里的第 2 行（0 基 1）")
        self.assertEqual(r.source_line + t.line + 1, 2)

    def test_lines_mode_reports_the_absolute_line_of_each_record(self):
        """One line per record: row 1 comes from paste line 1, row 2 from line 2.

        `lines` mode rebuilds `seg.records` after `segment()` ran, so the starts
        it would have produced have to be filled in again. Without that every
        record claims "记录内第 1 行" and the preview cannot be checked.
        """
        txt = "编号\t名称\nA1\t甲\nA2\t乙"
        cap = Captured(kind="text", payload=txt, channel="file", engine=None,
                       formats=["file"])
        card = parse_card({
            "schema": 1, "name": "t", "channel": "desktop",
            "segment": {"mode": "lines", "skip_header": True},
            "fields": {"编号": {"kind": "cell", "col": 0},
                       "名称": {"kind": "cell", "col": 1}},
        })
        res = parse_capture(cap, card)[0]
        self.assertEqual([r.source_line for r in res.rows], [1, 2])
        self.assertEqual(len(res.rows), 2, "表头行不能被当成一条记录")

    def test_a_search_rule_trace_names_the_record_line(self):
        """A rule that looks at the whole record must say which line it hit."""
        res, inf = parse_capture(_cap("sample-ecom.txt"), None)
        r = res.rows[0]
        spec = [s for s in inf.specs if s.name == "规格"][0]
        self.assertEqual(spec.kind, "search")
        t = r.traces["规格"]
        self.assertEqual(t.raw, "黑色 / 降噪版")
        self.assertEqual(t.line, 3,
                         "规格 在记录的第 4 行（0 基 3），不是记录开头")
        self.assertEqual(r.source_line + t.line + 1, 4)


class TestPreviewShape(unittest.TestCase):
    def test_grid_has_one_column_per_field_plus_source(self):
        res, _inf = parse_capture(_cap("sample-ecom.txt"), None)
        text = grid(res, limit=3).render()
        header = text.splitlines()[0]
        for f in res.fields:
            self.assertIn(f, header)
        self.assertIn("原文行", header)

    def test_numbered_source_marks_claimed_and_unclaimed_lines(self):
        res, _inf = parse_capture(_cap("sample-ecom.txt"), None)
        lines = (FIX / "sample-ecom.txt").read_text(encoding="utf-8").splitlines()
        out = "\n".join(numbered_source(lines, res, limit=5))
        self.assertIn("第1条", out)

    def test_every_source_line_is_claimed_or_marked_unparsed(self):
        """No line may vanish, and none may be claimed by the wrong record.

        `A店` sits before the first anchor and `B店` sits inside the first
        record's span once the blank line is accounted for. This used to be
        wrong in a way that *looked* safe: every line inside a record was
        placed one line too early, so `B店` fell through to the `·` marker and
        asserting the marker kept the off-by-one alive.
        """
        txt = "A店\n名：甲\n\nB店\n名：乙"
        cap = Captured(kind="text", payload=txt, channel="file", engine=None,
                       formats=["file"])
        card = parse_card({"schema": 1, "name": "t", "channel": "desktop",
                           "segment": {"mode": "anchor", "anchor": "^名[:：]"},
                           "fields": {"店": {"kind": "search",
                                             "pattern": "^(.+?)店$"}}})
        res = parse_capture(cap, card)[0]
        out = "\n".join(numbered_source(txt.splitlines(), res, limit=10))
        self.assertNotIn("·", out, "映射完整时不该有没人认领的行")
        self.assertRegex(out, r"4\s+第1条\s+B店", "空白行之后 B店 仍属第 1 条记录")
        self.assertRegex(out, r"1\s+未识别\s+A店", "锚点之前的行进未识别")
        self.assertRegex(out, r"5\s+未识别\s+名：乙", "没有锚点接住的行不能凭空归给谁")

    def test_a_missing_mapping_shows_the_gap_marker(self):
        """When nothing knows the source line, say `·` instead of guessing."""
        txt = "甲\n乙\n丙"
        cap = Captured(kind="text", payload=txt, channel="file", engine=None,
                       formats=["file"])
        card = parse_card({"schema": 1, "name": "t", "channel": "desktop",
                           "segment": {"mode": "lines"},
                           "fields": {"x": {"kind": "line", "index": 0}}})
        res = parse_capture(cap, card)[0]
        for r in res.rows:                    # deliberately forget the mapping
            r.source_line = None
            r.raw_at = []
        out = "\n".join(numbered_source(txt.splitlines(), res, limit=10))
        self.assertIn("·", out, "不知道来源时必须显示 `·`，不能凭空猜一个行号")


def _card_from(fixture: str, *, channel: str = "desktop"):
    """Build a card the way the wizard does, without asking anything."""
    cap = _cap(fixture)
    res, inf = parse_capture(cap, None)
    with redirect_stdout(io.StringIO()):
        ans = taskflow.build_answers(
            res, inf, cap, argparse.Namespace(yes=True, rows=3, channel="auto"))
    text = taskflow.render_task_card(ans)
    return parse_card(tomllib.loads(text), "wizard.toml")


class TestWizard(unittest.TestCase):
    """从样本生成可直接执行的卡片."""

    def test_generated_card_parses_and_reproduces_the_sample(self):
        cap = _cap("sample-ecom.txt")
        res_infer, inf = parse_capture(cap, None)
        card = _card_from("sample-ecom.txt")
        res_card, _ = parse_capture(cap, card)
        self.assertEqual(res_card.fields, res_infer.fields,
                         "向导生成的卡必须复现 infer 的列名")
        self.assertEqual([r.values for r in res_card.rows],
                         [r.values for r in res_infer.rows],
                         "向导生成的卡必须复现 infer 的每一格")

    def test_generated_card_has_a_task_block(self):
        card = _card_from("sample-ecom.txt")
        self.assertTrue(card.task.get("title"))
        self.assertEqual(card.title, card.task["title"])

    def test_a_table_sample_keeps_its_header_out_of_the_records(self):
        """A card in `lines` mode must skip the header, or it becomes a record.

        The wizard is the only place that knows whether `infer` dropped a header
        row, and leaving `skip_header` off puts that row back as data whose first
        cell is the literal word "订单号".
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sample.txt"
            p.write_text("订单号\t客户\nA1\t甲\nA2\t乙\n", encoding="utf-8")
            cap = Captured(kind="text", payload=p.read_text(encoding="utf-8"),
                           channel="file", engine=None, formats=["file"])
            res, inf = parse_capture(cap, None)
            with redirect_stdout(io.StringIO()):
                ans = taskflow.build_answers(
                    res, inf, cap, argparse.Namespace(yes=True, rows=1,
                                                      channel="auto"))
            self.assertTrue(ans.skip_header)
            card = parse_card(tomllib.loads(taskflow.render_task_card(ans)))
            out = parse_capture(cap, card)[0]
            self.assertEqual(len(out.rows), 2)
            self.assertEqual(out.rows[0].values["订单号"], "A1")

    def test_renaming_a_field_keeps_its_rule(self):
        cap = _cap("sample-ecom.txt")
        res, inf = parse_capture(cap, None)
        specs = taskflow._align_specs_to(res, inf.specs)
        renamed = taskflow._rename_spec("日期", "下单日期", specs)
        self.assertEqual(renamed.name, "下单日期")
        self.assertEqual(renamed.kind, specs[1].kind)
        self.assertEqual(renamed.index, specs[1].index)

    def test_duplicate_field_names_are_refused(self):
        """Two columns cannot share one header: the export would overwrite."""
        res, inf = parse_capture(_cap("sample-ecom.txt"), None)
        specs = taskflow._align_specs_to(res, inf.specs)
        real = taskflow._ask

        def same_name(prompt, default="", *, assume=False):
            return "同名"

        try:
            taskflow._ask = same_name  # type: ignore[assignment]
            with self.assertRaises(taskflow.InputError) as cm:
                taskflow._ask_fields(res, specs, assume=True)
            self.assertIn("字段名重复", str(cm.exception))
        finally:
            taskflow._ask = real  # type: ignore[assignment]

    def test_non_interactive_without_yes_is_an_error_not_a_guess(self):
        res, inf = parse_capture(_cap("sample-ecom.txt"), None)
        old = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            sys.stdin.isatty = lambda: False  # type: ignore[method-assign]
            with self.assertRaises(Exception) as cm:
                taskflow.build_answers(res, inf, _cap("sample-ecom.txt"),
                                       argparse.Namespace(yes=False, rows=1,
                                                          channel="auto"))
            self.assertIn("--yes", str(getattr(cm.exception, "hint", "")))
        finally:
            sys.stdin = old

    def test_written_card_is_valid_toml_and_reloadable(self):
        with tempfile.TemporaryDirectory() as d:
            cap = _cap("sample-ecom.txt")
            res, inf = parse_capture(cap, None)
            with redirect_stdout(io.StringIO()):
                ans = taskflow.build_answers(
                    res, inf, cap, argparse.Namespace(yes=True, rows=1,
                                                      channel="auto"))
            text = taskflow.render_task_card(ans)
            p = Path(d) / "t.toml"
            p.write_text(text, encoding="utf-8")
            card = parse_card(tomllib.loads(p.read_text(encoding="utf-8")), str(p))
            self.assertEqual(card.name, ans.name)


class TestConvertingACardKeepsWhatItAlreadySaid(unittest.TestCase):
    """`task new -p` 是加任务元数据，不是重写这张卡.

    The conversion used to rebuild the `[task]` block from the inference, so
    `--yes` replaced a hand-written purpose and source with an empty string and
    a guess, and the `[ledger]` section was not carried at all. Nothing failed:
    the converted card produced the same rows and then read them as "whatever we
    saw last wins", which is the failure the ledger section exists to stop.
    """

    def _card(self):
        return parse_card({
            "schema": 1, "name": "orders", "channel": "desktop",
            "task": {"title": "订单台账", "purpose": "整理成能核对的台账",
                     "source": "订单后台 → 订单列表", "created": "2026-10-25"},
            "segment": {"mode": "lines"},
            "fields": {"订单号": {"kind": "cell", "col": 0},
                       "状态": {"kind": "cell", "col": 1},
                       "更新时间": {"kind": "cell", "col": 2}},
            "dedupe": {"key": ["订单号"], "mutable": ["状态", "更新时间"]},
            "ledger": {"sort": "business_time", "field": "更新时间",
                       "timezone": "Asia/Shanghai"},
        })

    def _convert(self, card):
        cap = Captured(kind="text", payload="A1\t待付款\t2026-03-10 09:00",
                       channel="file")
        res, inf = parse_capture(cap, card)
        with redirect_stdout(io.StringIO()):
            answers = taskflow.build_answers(
                res, inf, cap, argparse.Namespace(yes=True), card)
        return parse_card(tomllib.loads(taskflow.render_task_card(answers)))

    def test_the_purpose_and_source_survive_yes(self):
        saved = self._convert(self._card())
        self.assertEqual(saved.task.get("purpose"), "整理成能核对的台账")
        self.assertEqual(saved.task.get("source"), "订单后台 → 订单列表")
        self.assertEqual(saved.task.get("title"), "订单台账")

    def test_the_date_the_task_started_is_not_restamped(self):
        saved = self._convert(self._card())
        self.assertEqual(saved.task.get("created"), "2026-10-25")

    def test_the_ledger_rule_survives(self):
        saved = self._convert(self._card())
        self.assertEqual(saved.ledger_sort, "business_time")
        self.assertEqual(saved.ledger_sort_field, "更新时间")
        self.assertEqual(saved.ledger.get("timezone"), "Asia/Shanghai")

    def test_a_card_without_a_ledger_still_converts_without_one(self):
        card = self._card()
        card.ledger = {}
        saved = self._convert(card)
        self.assertEqual(saved.ledger, {})

    def test_key_types_survive_the_round_trip(self):
        # `key_types` decides whether "0012" and "12" are the same order. It is
        # written as an inline table, which is the one part of `[ledger]` that
        # a hand-rolled writer can silently mangle into invalid TOML.
        card = self._card()
        card.ledger["key_types"] = {"订单号": "int"}
        saved = self._convert(card)
        self.assertEqual(saved.ledger.get("key_types"), {"订单号": "int"})


class TestFix(unittest.TestCase):
    """一次修正可以预览整批并保存."""

    def test_conversion_keeps_composite_keys_and_renames_all_references(self):
        from unittest.mock import patch
        from dataclasses import replace
        cap = Captured(kind="text", payload="A\t1\told\nA\t2\tnew", channel="file")
        card = parse_card({"schema": 1, "name": "composite", "channel": "desktop",
                           "segment": {"mode": "lines", "required": ["id", "part"]},
                           "fields": {"id": {"kind": "cell", "col": 0},
                                      "part": {"kind": "cell", "col": 1},
                                      "status": {"kind": "cell", "col": 2}},
                           "dedupe": {"key": ["id", "part"], "on_collision": "keep_all",
                                      "identity": ["id"], "mutable": ["status"]}})
        res, inf = parse_capture(cap, card)
        renamed = [replace(f, name="new_" + f.name) for f in card.fields]
        with patch.object(taskflow, "_ask_fields", return_value=renamed), redirect_stdout(io.StringIO()):
            answers = taskflow.build_answers(res, inf, cap, argparse.Namespace(yes=True), card)
        saved = parse_card(tomllib.loads(taskflow.render_task_card(answers)))
        self.assertEqual(saved.dedupe_key, ["new_id", "new_part"])
        self.assertEqual(saved.on_collision, "keep_all")
        self.assertEqual(saved.identity_fields, ["new_id"])
        self.assertEqual(saved.mutable_fields, ["new_status"])
        self.assertEqual(saved.required, ["new_id", "new_part"])
        self.assertEqual(len(parse_capture(cap, saved)[0].rows), 2)

    def test_switching_to_search_clears_the_stale_index(self):
        from clip2table.pipeline.extract import FieldSpec
        before = FieldSpec(name="f", kind="line", index=4)
        after = taskflow.Fix(field="f", kind="search", pattern=r"(\d+)").apply(before)
        self.assertEqual(after.kind, "search")
        self.assertEqual(after.index, 0, "换原语后残留的 index 会悄悄取错行")
        self.assertIsNone(after.col)

    def test_switching_to_cell_clears_the_pattern(self):
        from clip2table.pipeline.extract import FieldSpec
        before = FieldSpec(name="f", kind="search", pattern=r"(\d+)")
        after = taskflow.Fix(field="f", kind="cell", col=2).apply(before)
        self.assertEqual(after.kind, "cell")
        self.assertIsNone(after.pattern)

    def test_search_without_a_pattern_is_refused(self):
        from clip2table.pipeline.extract import FieldSpec
        with self.assertRaises(Exception):
            taskflow.Fix(field="f", kind="search").apply(
                FieldSpec(name="f", kind="line", index=0))

    def test_effect_is_reported_per_record(self):
        cap = _cap("sample-ecom.txt")
        card = _card_from("sample-ecom.txt")
        before, _ = parse_capture(cap, card)
        idx = card.field_names.index("line5")
        fixed = list(card.fields)
        fixed[idx] = taskflow.Fix(field="line5", kind="search",
                                  pattern=r"有用[（(](\d+)").apply(fixed[idx])
        from clip2table.cards.schema import Card
        after, _ = parse_capture(cap, Card(**{**card.__dict__, "fields": fixed}))
        lines = taskflow.show_effect(before, after, "line5")
        self.assertTrue(any("→" in ln for ln in lines),
                        "修正必须逐条说明改成了什么")
        self.assertEqual(after.rows[0].values["line5"], "128")
        self.assertEqual(after.check_conservation(), None, "修正不许破坏守恒")

    def test_a_fix_that_empties_values_says_so(self):
        cap = _cap("sample-ecom.txt")
        card = _card_from("sample-ecom.txt")
        before, _ = parse_capture(cap, card)
        idx = card.field_names.index("line5")
        fixed = list(card.fields)
        # A pattern that cannot match: honest about breaking, not silently blank.
        fixed[idx] = taskflow.Fix(field="line5", kind="search",
                                  pattern=r"不可能出现的文字(\d+)").apply(fixed[idx])
        from clip2table.cards.schema import Card
        after, _ = parse_capture(cap, Card(**{**card.__dict__, "fields": fixed}))
        lines = taskflow.show_effect(before, after, "line5")
        self.assertTrue(any("空了" in ln for ln in lines))


class TestTaskStats(unittest.TestCase):
    """首次耗时、修正次数、再次使用情况."""

    def test_first_run_seconds_are_kept_apart_from_later_runs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            tasks.record_run(p, rows=5, pages=1, ok=True, seconds=12.5)
            tasks.record_run(p, rows=9, pages=2, ok=True)
            s = tasks.summarise(tasks.load(p))
            self.assertEqual(s["runs"], 2)
            self.assertEqual(s["first_seconds"], 12.5,
                             "首次耗时只记第一次，混入后续会被平均掉")
            self.assertEqual(s["last_rows"], 9)

    def test_fixes_count_and_clean_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            tasks.record_run(p, rows=5, pages=1, ok=True, seconds=3.0)
            tasks.record_fix(p, field="日期", kind="rule")
            tasks.record_run(p, rows=5, pages=1, ok=True)
            s = tasks.summarise(tasks.load(p))
            self.assertEqual(s["fixes"], 1)
            self.assertEqual(s["clean_reruns"], 0, "上一次修正之后的第一次重跑不算干净")

    def test_describe_is_empty_before_the_first_run(self):
        self.assertEqual(tasks.describe(tasks.summarise({"runs": [], "fixes": []})), [])

    def test_a_corrupt_stats_file_never_blocks_a_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            p.write_text("{not json", encoding="utf-8")
            s = tasks.summarise(tasks.load(p))
            self.assertEqual(s["runs"], 0)
            tasks.record_run(p, rows=1, pages=1, ok=True)
            self.assertTrue(json.loads(p.read_text(encoding="utf-8"))["runs"])

    def test_stats_path_is_per_dataset_or_per_card(self):
        self.assertEqual(tasks.stats_path("out/orders").name, "task-stats.json")
        self.assertEqual(tasks.stats_path("cards/t.toml").name, "t.stats.json")

    def test_a_fix_and_a_run_land_in_the_same_file(self):
        """`task show` must see what `task fix` recorded.

        A run into a dataset used to be recorded inside the dataset directory
        while fixes went beside the card. `task show` then printed "还没改过
        规则" forever, and "reruns that needed no fix" was always zero because
        `prev["fixes"]` read a file the fix had never touched.
        """
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "orders.toml"
            card.write_text("schema = 1\n", encoding="utf-8")
            p = tasks.stats_path(card)
            tasks.record_run(p, rows=3, pages=1, ok=True, seconds=1.0)
            tasks.record_fix(p, field="金额", kind="search", detail="x → y")
            tasks.record_run(p, rows=5, pages=2, ok=True)
            s = tasks.summarise(tasks.load(p))
            self.assertEqual((s["runs"], s["fixes"]), (2, 1))
            self.assertEqual(s["clean_reruns"], 0)
            self.assertEqual(s["first_seconds"], 1.0)

    def test_each_run_keeps_its_own_seconds(self):
        """Every run carries its own timing; there is no "one" duration.

        Keeping only the first run's seconds looked like protection against an
        average, but it answers a question nobody asks: the first run reads one
        page into an empty table, and the twentieth reads one page into a table
        with thousands of rows. Losing the later numbers makes the trend
        invisible, which is the thing worth watching.
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            tasks.record_run(p, rows=1, pages=1, ok=True, seconds=120.0)
            tasks.record_run(p, rows=2, pages=1, ok=True, seconds=0.4)
            runs = tasks.load(p)["runs"]
            self.assertEqual(runs[0]["seconds"], 120.0)
            self.assertEqual(runs[1]["seconds"], 0.4)
            s = tasks.summarise(tasks.load(p))
            self.assertEqual(s["first_seconds"], 120.0)
            self.assertEqual(s["last_seconds"], 0.4)

    def test_confirmation_records_how_long_it_took_to_get_it_right(self):
        """"Configured to confirmed correct" is the cost of a new task.

        It is measured from `created.at`, so it includes the fixing and the
        re-reading -- the parts a per-run timer cannot see.
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            tasks.save(p, {"created": {"at": "2026-01-01T10:00:00+00:00"},
                           "runs": [], "fixes": []})
            s = tasks.record_confirm(p, rows=11, run=1)
            self.assertEqual(s["confirmed"], 1)
            self.assertIsNotNone(s["setup_seconds"])
            self.assertGreater(s["setup_seconds"], 0)
            self.assertEqual(tasks.load(p)["confirmations"][0]["run"], 1)

    def test_a_task_created_elsewhere_has_no_setup_figure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            s = tasks.record_confirm(p, rows=3)
            self.assertEqual(s["confirmed"], 1)
            self.assertIsNone(s["setup_seconds"],
                              "不知道什么时候建的，就不能编一个耗时出来")

    def test_an_unwritable_record_does_not_raise(self):
        """The pages are the product; the note about them is not worth a crash."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.stats.json"
            p.mkdir()                      # a directory where the file should be
            s = tasks.record_run(p, rows=3, pages=1, ok=True, seconds=1.0)
            self.assertFalse(s["recorded"])
            self.assertEqual(s["runs"], 1)  # the in-memory record still counts
            self.assertFalse(tasks.record_fix(p, field="a", kind="line"))

    def test_seconds_read_like_seconds(self):
        self.assertEqual(tasks._secs(0.4), "不到 1 秒")
        self.assertEqual(tasks._secs(12.6), "13 秒")
        self.assertEqual(tasks._secs(64), "1 分 04 秒")


class TestTaskSection(unittest.TestCase):
    """`[task]` must not change how anything parses."""

    def test_unknown_task_key_is_a_named_error(self):
        data = {"schema": 1, "name": "t", "channel": "desktop",
                "segment": {"mode": "lines"},
                "fields": {"a": {"kind": "line", "index": 0}},
                "task": {"titel": "typo"}}
        with self.assertRaises(Exception) as cm:
            parse_card(data, "x.toml")
        self.assertIn("titel", str(cm.exception))

    def test_a_card_without_task_still_parses(self):
        data = {"schema": 1, "name": "t", "channel": "desktop",
                "segment": {"mode": "lines"},
                "fields": {"a": {"kind": "line", "index": 0}}}
        card = parse_card(data, "x.toml")
        self.assertEqual(card.task, {})
        self.assertEqual(card.title, "t")

    def test_expected_accepts_a_string_or_a_list(self):
        base = {"schema": 1, "name": "t", "channel": "desktop",
                "segment": {"mode": "lines"},
                "fields": {"a": {"kind": "line", "index": 0}}}
        one = parse_card({**base, "task": {"expected": "一句话"}}, "x.toml")
        self.assertEqual(one.task["expected"], ["一句话"])
        two = parse_card({**base, "task": {"expected": ["a", "b"]}}, "x.toml")
        self.assertEqual(two.task["expected"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
