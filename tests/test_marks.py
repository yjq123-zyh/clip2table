"""Exercise record and field marks through the saved-task commands."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from clip2table import marks
from clip2table.cards.loader import load_card
from clip2table.cli import main
from clip2table.core import parse_capture
from clip2table.errors import BAD_INPUT, InputError, OK
from clip2table.models import Captured


SOURCE = "后台订单\n\nORDER-1001\n\n10.00\n待发货\nORDER-1002\n20.00\n已完成\n"


def capture(text: str) -> Captured:
    return Captured(kind="text", payload=text, channel="file")


def run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def write_card(folder: Path, *, mode: str = "anchor", kind: str = "line") -> Path:
    rule = {"line": 'index = 1', "search": 'pattern = "^missing$"',
            "cell": 'col = 0', "const": 'value = "0"'}[kind]
    card = folder / "source.toml"
    card.write_text(
        f'''schema = 1
name = "marked-orders"
channel = "desktop"
[segment]
mode = "{mode}"
''' + ("anchor = '^ORDER-\\d+$'\n" if mode == "anchor" else "") + f'''
[fields.order]
kind = "line"
index = 0
[fields.amount]
kind = "{kind}"
{rule}
type = "float"
multiplier = 2
[fields.status]
kind = "line"
index = 2
''', encoding="utf-8")
    return card


class TestMarks(unittest.TestCase):
    def test_marked_boundaries_save_and_reuse_with_headers_and_blanks(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "page.txt"
            source.write_text(SOURCE, encoding="utf-8")
            original = write_card(folder, mode="single")
            saved = folder / "saved.toml"
            code, out, err = run_cli(
                "task", "new", "-p", str(original), "--text-file", str(source),
                "--record-start", "3", "--record-end", "6", "--yes",
                "--out", str(saved))
            self.assertEqual(code, OK, out + err)
            card = load_card(saved)
            self.assertEqual(card.record_lines, (3, 3))
            for text in (SOURCE, SOURCE.replace("1001", "2001").replace("1002", "2002")):
                with self.subTest(text=text):
                    result, _ = parse_capture(capture(text), card)
                    self.assertEqual([r.values["amount"] for r in result.rows], [20.0, 40.0])
                    self.assertEqual([r.raw_at for r in result.rows], [[2, 4, 5], [6, 7, 8]])
                    self.assertEqual(result.unparsed, ["后台订单"])
                    self.assertIsNone(result.check_conservation())
                    self.assertFalse(result.deviations)
            code, out, err = run_cli("task", "check", str(saved), "--text-file",
                                     str(source), "--json")
            self.assertEqual(code, OK, err)
            self.assertEqual([r["amount"]["line"] for r in json.loads(out)["sources"]], [5, 8])

    def test_invalid_boundaries_and_incomplete_anchors_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "page.txt"
            source.write_text(SOURCE, encoding="utf-8")
            saved = folder / "saved.toml"
            for options in (("--record-start", "3"), ("--record-end", "6"),
                            ("--record-start", "0", "--record-end", "6"),
                            ("--record-start", "6", "--record-end", "3"),
                            ("--record-start", "3", "--record-end", "99")):
                with self.subTest(options=options):
                    code, out, err = run_cli("task", "new", "--text-file", str(source),
                                             "--out", str(saved), "--yes", *options)
                    self.assertEqual(code, BAD_INPUT, out + err)
                    self.assertFalse(saved.exists())
            with self.assertRaisesRegex(InputError, "漏掉"):
                marks.anchor_from(["订单1", "10", "订单X", "20", "订单3", "30"], 0, 2)
            with self.assertRaisesRegex(InputError, "不该匹配"):
                marks.anchor_from(["订单1", "订单2", "订单3", "订单4"], 0, 2)

    def test_field_marks_switch_primitive_and_persist_for_the_entire_batch(self):
        for kind in ("search", "cell", "const"):
            for option in (("--from-line", "8"), ("--value", "10.00")):
                with self.subTest(kind=kind, option=option), tempfile.TemporaryDirectory() as temp:
                    folder = Path(temp)
                    card_path = write_card(folder, kind=kind)
                    source = folder / "page.txt"
                    source.write_text(SOURCE, encoding="utf-8")
                    code, out, err = run_cli(
                        "task", "fix", str(card_path), "--field", "amount",
                        "--text-file", str(source), *option, "--write")
                    self.assertEqual(code, OK, out + err)
                    card = load_card(card_path)
                    amount = next(f for f in card.fields if f.name == "amount")
                    self.assertEqual((amount.kind, amount.index, amount.multiplier), ("line", 1, 2))
                    result, _ = parse_capture(capture(SOURCE), card)
                    self.assertEqual([r.values["amount"] for r in result.rows], [20.0, 40.0])
                    self.assertIsNone(result.check_conservation())

    def test_invalid_field_marks_leave_the_card_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            card = write_card(folder)
            original = card.read_bytes()
            source = folder / "page.txt"
            source.write_text(SOURCE, encoding="utf-8")
            for options in (("--from-line", "0"), ("--from-line", "1"),
                            ("--from-line", "4"), ("--from-line", "99"),
                            ("--value", ""), ("--value", "20.00"),
                            ("--from-line", "5", "--kind", "cell")):
                with self.subTest(options=options):
                    code, out, err = run_cli("task", "fix", str(card), "--field", "amount",
                                             "--text-file", str(source), "--write", *options)
                    self.assertEqual(code, BAD_INPUT, out + err)
                    self.assertEqual(card.read_bytes(), original)
            with self.assertRaises(InputError):
                marks.find_in_record(["10.00", "实付10.00"], "10.00")

    def test_picked_candidate_preserves_json_source_positions_and_the_card(self):
        from clip2table.pipeline.infer import infer

        lines = (["名称：甲", "价格：10", "状态：在售"] * 5
                 + ["标题X", "2026-01-01", "正文内容在这里", "规格：红", "有用(3)"] * 6)
        text = "\n\n".join(lines)
        alternate = infer(capture(text)).alternates[0]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            card = write_card(folder, mode="single")
            original = card.read_bytes()
            source = folder / "page.txt"
            source.write_text(text, encoding="utf-8")
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(source),
                                     "--pick", "1", "--json", "--rows", "100")
            self.assertEqual(code, OK, err)
            data = json.loads(out)
            self.assertEqual(data["rows"], alternate.record_count)
            self.assertEqual([r["order"] for r in data["samples"]], [r[0] for r in alternate.sample])
            self.assertEqual(data["sources"][0]["order"]["line"], alternate.offset * 2 + 1)
            self.assertIsNone(data["conservation"])
            self.assertEqual(card.read_bytes(), original)

            code, out, err = run_cli("task", "check", str(card), "--text-file", str(source),
                                     "--pick", "1", "--write", "--confirm", "--json", "--rows", "100")
            self.assertEqual(code, OK, err)
            saved = json.loads(out)
            self.assertTrue(saved["saved"])
            self.assertTrue(saved["confirmed"])
            code, out, err = run_cli("task", "check", str(card), "--text-file", str(source),
                                     "--json", "--rows", "100")
            self.assertEqual(code, OK, err)
            reused = json.loads(out)
            for key in ("samples", "sources", "rows", "conservation", "unparsed", "confirmed"):
                self.assertEqual(reused[key], saved[key], key)
            self.assertEqual(load_card(card).segment_mode, "periodic")

    def test_line_pattern_survives_type_fix_and_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            card = write_card(folder)
            text = card.read_text(encoding="utf-8").replace(
                "index = 1", "index = 1\npattern = '([0-9.]+)'")
            card.write_text(text, encoding="utf-8")
            source = folder / "page.txt"
            source.write_text(SOURCE.replace("10.00", "金额：10.00").replace("20.00", "金额：20.00"), encoding="utf-8")
            code, out, err = run_cli("task", "fix", str(card), "--field", "amount",
                                     "--type", "float", "--text-file", str(source), "--write")
            self.assertEqual(code, OK, out + err)
            res = parse_capture(capture(source.read_text(encoding="utf-8")), load_card(card))[0]
            self.assertEqual([r.values["amount"] for r in res.rows], [20.0, 40.0])
            converted = folder / "converted.toml"
            code, out, err = run_cli("task", "new", "-p", str(card), "--text-file", str(source),
                                     "--yes", "--out", str(converted))
            self.assertEqual(code, OK, out + err)
            self.assertEqual(parse_capture(capture(source.read_text(encoding="utf-8")), load_card(converted))[0].rows,
                             res.rows)

    def test_periodic_boundaries_and_invalid_configuration(self):
        from clip2table.cards.schema import parse_card
        base = {"schema": 1, "name": "periodic", "channel": "desktop", "fields": {"v": {"kind": "line", "index": 0}}}
        for seg in ({"period": 0}, {"period": True}, {"period": 2, "offset": -1},
                    {"period": 2, "count": 0}):
            with self.subTest(seg=seg), self.assertRaises(InputError):
                parse_card({**base, "segment": {"mode": "periodic", **seg}})
        card = parse_card({**base, "segment": {"mode": "periodic", "period": 2, "offset": 1, "count": 2}})
        for text, values, unparsed in (("header\n\na\n1\nb\n2\nfooter", ["a", "b"], ["header", "footer"]),
                                       ("header\nc\n3", ["c"], ["header"]), ("header", [], ["header"]), ("", [], [])):
            with self.subTest(text=text):
                res = parse_capture(capture(text), card)[0]
                self.assertEqual([r.values["v"] for r in res.rows], values)
                self.assertEqual(res.unparsed, unparsed)
                self.assertIsNone(res.check_conservation())


if __name__ == "__main__":
    unittest.main()
