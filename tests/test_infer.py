"""The infer ladder: S0 through S4, in that order, and S4 is a real answer.

What the ladder must never do is invent structure that the source does not
have. "There is no record boundary here, so here are the lines" is a truthful
answer; a fabricated three-column table is not.
"""
from __future__ import annotations

import unittest

from clip2table.models import Captured
from clip2table.pipeline.infer import infer


def text(payload: str) -> Captured:
    return Captured(kind="text", payload=payload, channel="file", formats=["file"])


def html(payload: str) -> Captured:
    return Captured(kind="html", payload=payload, channel="file", formats=["file"])


# One record, four lines, repeated. Title / date / body / count.
BLOCK = ["星海无线耳机 Pro", "2026-03-14", "包装完好，降噪比上一代强不少", "有用(128)"]


def blocks(n: int) -> str:
    out = []
    for i in range(n):
        out.append(f"{BLOCK[0]} {i}")
        out.extend(BLOCK[1:])
    return "\n".join(out)


class TestS0Tables(unittest.TestCase):
    def test_tsv_with_header_uses_the_header_as_field_names(self):
        cap = text("订单号\t日期\t金额\nA1\t2026-01-01\t99\nA2\t2026-01-02\t120")
        inf = infer(cap)
        self.assertEqual(inf.level, "S0")
        self.assertEqual(inf.result.fields, ["订单号", "日期", "金额"])
        self.assertEqual(len(inf.result.rows), 2)
        self.assertEqual(inf.result.rows[0].values["金额"], "99")

    def test_tsv_without_a_header_names_columns_positionally(self):
        cap = text("1\t2\t3\n4\t5\t6\n7\t8\t9")
        inf = infer(cap)
        self.assertEqual(inf.level, "S0")
        self.assertEqual(inf.result.fields, ["col1", "col2", "col3"])
        self.assertEqual(len(inf.result.rows), 3)

    def test_html_table_becomes_the_same_shape(self):
        page = ("<table>"
                "<tr><th>名称</th><th>价格</th></tr>"
                "<tr><td>咖啡机</td><td>899</td></tr>"
                "<tr><td>耳机</td><td>1299</td></tr>"
                "</table>")
        inf = infer(html(page))
        self.assertEqual(inf.level, "S0")
        self.assertEqual(inf.result.fields, ["名称", "价格"])
        self.assertEqual(len(inf.result.rows), 2)

    def test_s0_keeps_the_books_balanced(self):
        cap = text("a\tb\n1\t2\n3\t4")
        inf = infer(cap)
        self.assertIsNone(inf.result.check_conservation())


class TestS1Periodic(unittest.TestCase):
    def test_uniform_blocks_become_one_row_each(self):
        inf = infer(text(blocks(3)))
        self.assertEqual(inf.level, "S1")
        self.assertEqual(len(inf.result.rows), 3)
        self.assertEqual(inf.result.rows[0].values["日期"], "2026-03-14")

    def test_the_title_column_comes_first(self):
        # The boundary offset must land on the title, not on the date line:
        # a record starting at its date would shift every column by one.
        inf = infer(text(blocks(3)))
        first = inf.result.rows[0].values
        self.assertTrue(str(first[inf.result.fields[0]]).startswith("星海无线耳机"))

    def test_a_kv_column_is_named_from_its_own_key_and_stripped(self):
        payload = ("山雾便携咖啡机\n2026-03-08\n一般\n规格：黑色\n有用(37)\n"
                   "轻羽人体工学椅\n2026-03-11\n腰托到位\n规格：灰\n有用(56)\n"
                   "飞鸟台灯\n2026-03-09\n光线舒服\n规格：白\n有用(12)")
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S1")
        self.assertIn("规格", inf.result.fields)
        # The header says 规格, so the cell must not still say 规格：黑色.
        col = inf.result.fields.index("规格")
        self.assertEqual(list(inf.result.rows[0].values.values())[col], "黑色")

    def test_s1_keeps_the_books_balanced(self):
        inf = infer(text(blocks(4)))
        self.assertIsNone(inf.result.check_conservation())


class TestS2KeyValue(unittest.TestCase):
    def test_repeated_key_splits_into_records(self):
        payload = ("姓名：张三\n部门：研发\n姓名：李四\n部门：市场")
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S2")
        self.assertEqual(len(inf.result.rows), 2)
        self.assertEqual(inf.result.rows[0].values["姓名"], "张三")
        self.assertEqual(inf.result.rows[1].values["部门"], "市场")

    def test_a_missing_key_stays_empty_rather_than_borrowing(self):
        payload = "姓名：张三\n部门：研发\n姓名：李四\n姓名：王五\n部门：财务"
        inf = infer(text(payload))
        rows = inf.result.rows
        self.assertIsNone(rows[1].values.get("部门"))

    def test_single_key_value_pair_list_becomes_key_value_columns(self):
        payload = "项目：城东变电站\n进度：已完工\n投资：1200 万"
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S2")
        self.assertEqual(inf.result.fields, ["key", "value"])
        self.assertEqual(len(inf.result.rows), 3)

    def test_non_kv_lines_are_kept_as_unparsed(self):
        payload = "姓名：张三\n部门：研发\n—— 备注 ——\n姓名：李四\n部门：市场"
        inf = infer(text(payload))
        self.assertIn("—— 备注 ——", inf.result.unparsed)


class TestS3Numbered(unittest.TestCase):
    def test_numbered_lines_with_a_separator_split_into_columns(self):
        payload = ("1. 射频识别门禁改造 —— 2026-02 完成招标\n"
                   "2. 光纤入户二期 —— 2026-03 施工中\n"
                   "3. 智慧停车试点 —— 2026-01 已验收")
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S3")
        self.assertEqual(len(inf.result.rows), 3)
        self.assertEqual(len(inf.result.fields), 2)
        self.assertEqual(inf.result.rows[0].values["col1"], "射频识别门禁改造")

    def test_numbered_lines_without_a_separator_keep_the_text_intact(self):
        payload = "1. 第一条事项\n2. 第二条事项\n3. 第三条事项"
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S3")
        self.assertEqual(inf.result.fields, ["index", "text"])
        self.assertEqual(inf.result.rows[0].values["text"], "第一条事项")

    def test_chinese_numerals_count_as_numbering(self):
        payload = "一、立项\n二、评审\n三、实施"
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S3")

    def test_non_numbered_lines_are_kept_as_unparsed(self):
        payload = "会议通知\n1. 立项\n2. 评审\n3. 实施"
        inf = infer(text(payload))
        self.assertIn("会议通知", inf.result.unparsed)


class TestS4HonestFallback(unittest.TestCase):
    def test_prose_becomes_one_row_per_line_single_column(self):
        payload = ("这次现场核查主要看了三处：一是配电间的接地电阻，"
                   "实测 2.4 欧，低于 4 欧的限值；二是消防通道，畅通。")
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S4")
        self.assertEqual(inf.result.fields, ["raw"])
        self.assertFalse(inf.result.rows[0].values["raw"] == "")

    def test_s4_is_labelled_as_an_answer_not_a_failure(self):
        inf = infer(text("一句话。"))
        self.assertEqual(inf.level, "S4")
        self.assertTrue(any("不是失败" in n for n in inf.result.notes))

    def test_s4_keeps_the_books_balanced(self):
        inf = infer(text("第一句。\n第二句。\n第三句。"))
        self.assertIsNone(inf.result.check_conservation())

    def test_empty_payload_returns_an_empty_table(self):
        inf = infer(text(""))
        self.assertEqual(inf.result.rows, [])


class TestLadderOrder(unittest.TestCase):
    def test_a_credible_period_beats_a_kv_look(self):
        # Every line here is key:value shaped, but the block repeats every two
        # lines, so the period is the stronger signal: it keeps every line
        # inside a record. Both roads reach the same table here, but S1 gets
        # there without needing a repeated key to anchor on.
        payload = "名称：甲\n价格：1\n名称：乙\n价格：2\n名称：丙\n价格：3"
        inf = infer(text(payload))
        self.assertEqual(inf.level, "S1")
        self.assertEqual(len(inf.result.rows), 3)
        self.assertEqual(list(inf.result.fields), ["名称", "价格"])
        self.assertIsNone(inf.result.check_conservation())

    def test_a_table_beats_everything(self):
        cap = text("a\tb\n1\t2\n3\t4\n5\t6")
        self.assertEqual(infer(cap).level, "S0")


class TestAmbiguity(unittest.TestCase):
    """Two jobs, and only two: name the rival readings, and never invent one.

    A rival is worth a paragraph only when it places the same text. Rotating the
    boundary by one line inside an all-identical record list looks like a rival
    on every structural measure and is not one: it abandons the lines it skips
    over. Reporting it turned the section into a permanent "we cannot tell where
    records start" on text whose answer is just "at the first line of the run",
    which is how an honest warning becomes noise people learn to skip.
    """

    def test_a_clean_periodic_list_reports_no_rival(self):
        inf = infer(text(blocks(3)))
        self.assertEqual(inf.level, "S1")
        self.assertEqual(inf.alternates, [])
        self.assertFalse(inf.ambiguous)

    def test_a_second_table_in_the_same_text_is_named_as_a_rival(self):
        # A 3-line kv block followed by a 5-line list: two real tables, and the
        # tool can only return one. The other one is not a worse parse of the
        # same thing -- it is a different set of rows.
        kv = ["名称：甲", "价格：10", "状态：在售"] * 5
        lst = ["标题X", "2026-01-01", "正文内容在这里", "规格：红", "有用(3)"] * 6
        inf = infer(text("\n".join(kv + lst)))
        self.assertEqual(inf.level, "S1")
        self.assertTrue(inf.alternates, "同一段文本里的第二张表没有被报出来")
        rival = inf.alternates[0]
        self.assertEqual(rival.period, 3)
        self.assertEqual(rival.record_count, 6)
        self.assertIn("另一个周期", rival.why)
        self.assertTrue(rival.sample, "候选应当带一条示例记录供人对照")

    def test_the_rival_carries_its_evidence_not_a_confidence_score(self):
        kv = ["名称：甲", "价格：10", "状态：在售"] * 5
        lst = ["标题X", "2026-01-01", "正文内容在这里", "规格：红", "有用(3)"] * 6
        inf = infer(text("\n".join(kv + lst)))
        rival = inf.alternates[0]
        # Numbers a person can check against the text, not a percentage.
        self.assertIn("边界一致", rival.why)
        self.assertIn("同形", rival.why)

    def test_every_reading_states_why_it_was_chosen(self):
        inf = infer(text(blocks(3)))
        self.assertTrue(inf.evidence)
        self.assertIn("每 4 行一条", inf.evidence[0])
        self.assertIn("同形", inf.evidence[0])

    def test_a_leading_fragment_does_not_become_a_rival_rotation(self):
        # Three lines that are the tail of a record, then clean records. The
        # first three lines are unplaced; the reading "start three lines later"
        # is the same answer with the fragment dropped, and the fragment is
        # already reported as unparsed, so it is not a second answer.
        frag = ["正文零", "规格：蓝", "有用(9)"]
        body = ["标题A", "2026-01-01", "正文一", "规格：红", "有用(3)"] * 3
        inf = infer(text("\n".join(frag + body)))
        self.assertEqual(inf.level, "S1")
        for a in inf.alternates:
            self.assertNotIn("起点不唯一", a.why)


if __name__ == "__main__":
    unittest.main()
