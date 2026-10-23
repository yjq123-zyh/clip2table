"""The two claims a report makes about itself, pinned to disk.

1. "未识别的 N 行不会被丢弃" -- that is only true if the export actually writes
   them somewhere. The in-memory list dies with the process; a re-run on a
   changed page cannot reproduce them. If the sidecar is not written, the
   sentence is a lie, and the sentence is the whole reason the project exists.

2. The headline names the engine. `run --text-file x.txt` used to print
   `file ｜ HTTP`, which claims an HTTP fetch that never happened.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from clip2table.cli import main


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


# Two records with a navigation header that no anchor claims: 11 source lines,
# 9 consumed, 2 unparsed. Built here rather than loaded from fixtures/ so the
# arithmetic in the assertions is visible.
SAMPLE = (
    "导航 首页 | 关于我们\n"
    "分类：全部\n"
    "临江区市场监管局办公家具采购\n"
    "2026-01-15\n"
    "预算 48 万，含运输安装\n"
    "规格：胡桃木\n"
    "滨河区政务服务中心设备更新\n"
    "2026-02-03\n"
    "含三年质保\n"
    "规格：不锈钢\n"
    "页脚 版权所有\n"
)


class TestUnparsedSurvivesExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="c2t-unp-"))
        self.src = self.tmp / "in.txt"
        self.src.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sidecar_carries_the_exact_dropped_lines(self):
        out = self.tmp / "t.csv"
        code, stdout, _ = run_cli("run", "--text-file", str(self.src),
                                  "-p", "notice-list", "-f", "csv",
                                  "-o", str(out))
        self.assertEqual(code, 0)
        side = self.tmp / "t.csv.unparsed.txt"
        self.assertTrue(side.exists(), "unparsed lines were dropped on export")
        self.assertEqual(side.read_text(encoding="utf-8").splitlines(),
                         ["导航 首页 | 关于我们", "分类：全部"])
        # The table itself must stay clean: navigation is not a column.
        csv_text = out.read_text(encoding="utf-8-sig")
        self.assertNotIn("导航", csv_text)

    def test_report_line_points_at_the_sidecar(self):
        out = self.tmp / "t2.csv"
        _, stdout, _ = run_cli("run", "--text-file", str(self.src),
                               "-p", "notice-list", "-f", "csv", "-o", str(out))
        self.assertIn("未识别的 2 行不会被丢弃", stdout)
        self.assertIn(".unparsed.txt", stdout)

    def test_no_sidecar_when_nothing_is_dropped(self):
        clean = self.tmp / "clean.txt"
        clean.write_text(
            "临江区市场监管局办公家具采购\n2026-01-15\n预算 48 万\n规格：胡桃木\n",
            encoding="utf-8")
        out = self.tmp / "clean.csv"
        code, _, _ = run_cli("run", "--text-file", str(clean),
                             "-p", "notice-list", "-f", "csv", "-o", str(out))
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "clean.csv.unparsed.txt").exists())

    def test_sidecar_per_format_so_a_multi_format_export_keeps_them_apart(self):
        code, _, _ = run_cli("run", "--text-file", str(self.src),
                             "-p", "notice-list", "-f", "csv", "-f", "md",
                             "-o", str(self.tmp / "both.csv"))
        self.assertEqual(code, 0)
        sides = sorted(p.name for p in self.tmp.glob("*.unparsed.txt"))
        self.assertEqual(sides, ["both.csv.unparsed.txt", "both.md.unparsed.txt"])


class TestEngineLabelIsHonest(unittest.TestCase):
    def test_text_file_is_not_labelled_http(self):
        code, stdout, _ = run_cli("run", "--text-file", str(Path(
            __file__).resolve().parent.parent / "fixtures" / "sample-ecom.txt"),
            "-p", "generic-item-block", "--dry-run")
        self.assertEqual(code, 0)
        head = stdout.splitlines()[0]
        self.assertIn("｜ FILE ｜", head)
        self.assertNotIn("HTTP", head)

    def test_infer_on_a_file_is_not_labelled_http(self):
        code, stdout, _ = run_cli("infer", "--text-file", str(Path(
            __file__).resolve().parent.parent / "fixtures" / "sample-hr.txt"),
            "--dry-run")
        self.assertEqual(code, 0)
        self.assertNotIn("HTTP", stdout.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
