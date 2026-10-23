"""HTML table -> lines: cell POSITION is the fact being preserved.

The defect these tests exist for: `"\t".join(cells).strip()` deletes a leading
tab, and a leading tab is the only evidence that the first column is empty. A
value then reads out of the column before it, with no error anywhere.
"""
import unittest

from clip2table.pipeline.htmltable import html_to_grid, html_to_lines


class TestEmptyCells(unittest.TestCase):
    def test_leading_empty_cell_keeps_its_slot(self):
        lines = html_to_lines("<table><tr><td></td><td>甲</td><td>乙</td></tr></table>")
        self.assertEqual(lines, ["\t甲\t乙"])
        self.assertEqual(lines[0].split("\t"), ["", "甲", "乙"])

    def test_interior_empty_cell_keeps_its_slot(self):
        lines = html_to_lines("<table><tr><td>1</td><td></td><td>3</td></tr></table>")
        self.assertEqual(lines[0].split("\t"), ["1", "", "3"])

    def test_all_empty_row_is_not_a_record(self):
        lines = html_to_lines("<table><tr><td></td><td></td></tr>"
                              "<tr><td>a</td><td>b</td></tr></table>")
        self.assertEqual(lines, ["a\tb"])

    def test_cell_field_reads_the_right_column(self):
        # The end-to-end shape of the bug: column 2 must not become column 1.
        from clip2table.pipeline.extract import FieldSpec, extract
        lines = html_to_lines("<table><tr><td></td><td>甲</td><td>乙</td></tr></table>")
        row = extract(lines, [FieldSpec(name="c", kind="cell", col=1)])
        self.assertEqual(row.get("c"), "甲")


class TestSpans(unittest.TestCase):
    def test_rowspan_repeats_and_keeps_alignment(self):
        html = ("<table>"
                "<tr><td rowspan=2>A</td><td>B</td></tr>"
                "<tr><td>C</td></tr>"
                "</table>")
        self.assertEqual(html_to_lines(html), ["A\tB", "A\tC"])

    def test_colspan_keeps_following_columns_in_place(self):
        html = ("<table>"
                "<tr><td colspan=2>H</td><td>X</td></tr>"
                "<tr><td>a</td><td>b</td><td>c</td></tr>"
                "</table>")
        lines = html_to_lines(html)
        self.assertEqual(lines[0].split("\t"), ["H", "", "X"])
        # X is column 3 in both rows; without the placeholder it would be 2.
        self.assertEqual(lines[1].split("\t"), ["a", "b", "c"])

    def test_rowspan_placeholder_lands_at_its_column(self):
        html = ("<table>"
                "<tr><td>a</td><td rowspan=2>B</td><td>c</td></tr>"
                "<tr><td>d</td><td>f</td></tr>"
                "</table>")
        lines = html_to_lines(html)
        self.assertEqual(lines[1].split("\t"), ["d", "B", "f"])

    def test_nonsense_span_value_does_not_swallow_the_table(self):
        html = "<table><tr><td rowspan=0>x</td><td>y</td></tr></table>"
        self.assertEqual(html_to_lines(html), ["x\ty"])


class TestCellLineBreaks(unittest.TestCase):
    def test_br_inside_cell_stays_inside_the_cell(self):
        html = "<table><tr><td>第一行<br>第二行</td><td>B</td></tr></table>"
        self.assertEqual(html_to_grid(html), [["第一行\n第二行", "B"]])

    def test_lines_flatten_the_break_but_keep_one_record_per_line(self):
        html = "<table><tr><td>第一行<br>第二行</td><td>B</td></tr></table>"
        lines = html_to_lines(html)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "第一行 第二行\tB")


class TestGrid(unittest.TestCase):
    def test_grid_returns_rows_with_empty_cells(self):
        html = ("<table><tr><td></td><td>甲</td></tr>"
                "<tr><td>1</td><td>2</td></tr></table>")
        self.assertEqual(html_to_grid(html), [["", "甲"], ["1", "2"]])

    def test_paragraphs_and_tables_keep_their_order(self):
        html = "<p>标题</p><table><tr><td>a</td><td>b</td></tr></table><p>结尾</p>"
        self.assertEqual(html_to_lines(html), ["标题", "a\tb", "结尾"])


if __name__ == "__main__":
    unittest.main()
