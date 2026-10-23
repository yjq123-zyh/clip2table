"""CLI contract tests.

The CLI is the surface every user and every agent touches, so its contract is
what matters here: exit codes, which formats land on disk, and whether a
low-fill warning actually reaches stderr. Nothing here reads the real
clipboard - file input is the documented way to test deterministically.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from clip2table.cli import build_parser, main

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ECOM = FIXTURES / "sample-ecom.txt"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestParserShape(unittest.TestCase):
    def test_subcommands_exist(self):
        p = build_parser()
        actions = [a for a in p._subparsers._group_actions]  # noqa: SLF001
        names = set()
        for a in actions:
            names |= set(a.choices)
        for expected in ("run", "infer", "export", "cards", "doctor", "selftest"):
            self.assertIn(expected, names)

    def test_cards_has_list_show_validate(self):
        p = build_parser()
        tops = [a for a in p._subparsers._group_actions]  # noqa: SLF001
        choices = set()
        for a in tops:
            choices |= set(a.choices)
        self.assertIn("cards", choices)
        cards_p = p._subparsers._group_actions[0].choices["cards"]  # noqa: SLF001
        inner = set()
        for a in cards_p._subparsers._group_actions:  # noqa: SLF001
            inner |= set(a.choices)
        self.assertEqual(inner, {"list", "show", "validate"})

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            run_cli("-V")
        self.assertEqual(ctx.exception.code, 0)


class TestRunFromFile(unittest.TestCase):
    def test_csv_lands_on_disk_with_utf8_bom(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_cli("run", "-p", "generic-item-block",
                                     "--text-file", str(ECOM), "-f", "csv",
                                     "--dir", d)
            self.assertEqual(code, 0, err)
            files = list(Path(d).glob("*.csv"))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_all_three_formats_in_one_call(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_cli("run", "-p", "generic-item-block",
                                   "--text-file", str(ECOM),
                                   "-f", "csv", "-f", "md", "-f", "xlsx",
                                   "--dir", d)
            self.assertEqual(code, 0, err)
            exts = {p.suffix for p in Path(d).iterdir()}
            self.assertEqual(exts, {".csv", ".md", ".xlsx"})

    def test_no_format_means_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_cli("run", "-p", "generic-item-block",
                                   "--text-file", str(ECOM), "--dir", d)
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(d).iterdir()), [])
            self.assertIn("没写文件", out)

    def test_report_shows_channel_and_engine_first(self):
        code, out, _ = run_cli("run", "-p", "generic-item-block",
                               "--text-file", str(ECOM))
        self.assertEqual(code, 0)
        first = out.strip().splitlines()[0]
        self.assertIn("file", first)
        self.assertIn("行", first)

    def test_low_fill_rate_is_shown_not_hidden(self):
        # sample-gov has no 规格 field: spec must come out 0% and say so.
        gov = FIXTURES / "sample-gov.txt"
        code, out, _ = run_cli("run", "-p", "generic-item-block",
                               "--text-file", str(gov))
        self.assertEqual(code, 0)
        self.assertIn("spec 0%", out)
        self.assertIn("偏低", out)

    def test_json_report_is_parseable_and_carries_the_rows(self):
        code, out, _ = run_cli("run", "-p", "generic-item-block",
                               "--text-file", str(ECOM), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["rows"], 3)
        self.assertIn("title", data["fields"])
        self.assertEqual(data["conservation"], None)

    def test_out_path_overrides_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "custom.csv"
            code, _, err = run_cli("run", "-p", "generic-item-block",
                                   "--text-file", str(ECOM),
                                   "-o", str(target))
            self.assertEqual(code, 0, err)
            self.assertTrue(target.exists())

    def test_missing_input_file_is_a_bad_input_exit(self):
        code, _, err = run_cli("run", "--text-file", "does-not-exist.txt")
        self.assertEqual(code, 2)
        self.assertIn("找不到", err)


class TestInfer(unittest.TestCase):
    def test_tsv_becomes_s0(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            p.write_text("a\tb\n1\t2\n3\t4\n", encoding="utf-8")
            code, out, _ = run_cli("infer", "--text-file", str(p))
            self.assertEqual(code, 0)
            self.assertIn("S0", out)

    def test_prose_is_s4_and_says_it_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            p.write_text("这是一段没有结构的说明文字，只有一句话。", encoding="utf-8")
            code, out, _ = run_cli("infer", "--text-file", str(p))
            self.assertEqual(code, 0)
            self.assertIn("S4", out)

    def test_write_card_emits_a_loadable_toml(self):
        from clip2table.cards.loader import load_card
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            p.write_text(ECOM.read_text(encoding="utf-8"), encoding="utf-8")
            card_path = Path(d) / "draft.toml"
            code, _, err = run_cli("infer", "--text-file", str(p),
                                   "--write-card", str(card_path))
            self.assertEqual(code, 0, err)
            self.assertTrue(card_path.exists())
            card = load_card(card_path)
            self.assertTrue(card.fields)

    def test_draft_card_with_chinese_column_names_is_valid_toml(self):
        # `[fields.日期]` is not a valid bare key. An earlier draft emitted it
        # unquoted, producing a file the tool could not read back.
        from clip2table.cards.loader import load_card
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            p.write_text(ECOM.read_text(encoding="utf-8"), encoding="utf-8")
            card_path = Path(d) / "draft.toml"
            run_cli("infer", "--text-file", str(p), "--write-card", str(card_path))
            card = load_card(card_path)
            self.assertIn("日期", card.field_names)

    def test_draft_card_never_emits_a_match_anything_anchor_next(self):
        # '.+' next to an anchor is just a single-line anchor wearing a
        # disguise - the exact rule this project set out to replace.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            p.write_text(ECOM.read_text(encoding="utf-8"), encoding="utf-8")
            card_path = Path(d) / "draft.toml"
            run_cli("infer", "--text-file", str(p), "--write-card", str(card_path))
            text = card_path.read_text(encoding="utf-8")
            self.assertNotIn("'.+'", text)
            self.assertNotIn('".+"', text)

    def test_draft_card_reproduces_what_infer_printed(self):
        # The strongest statement of self-consistency: feeding the draft card
        # back in must yield the same table infer reported, not a shifted one.
        import json as _json
        from clip2table.cards.loader import load_card
        from clip2table.core import parse_capture
        from clip2table.models import Captured
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.txt"
            payload = ECOM.read_text(encoding="utf-8")
            p.write_text(payload, encoding="utf-8")
            card_path = Path(d) / "draft.toml"
            code, out, _ = run_cli("infer", "--text-file", str(p),
                                   "--write-card", str(card_path), "--json")
            self.assertEqual(code, 0)
            inf = _json.loads(out)
            cap = Captured(kind="text", payload=payload, channel="file")
            res, _ = parse_capture(cap, load_card(card_path))
            self.assertEqual(len(res.rows), inf["rows"])
            self.assertEqual(res.fields, inf["fields"])
            for got, want in zip(res.rows, inf["sample"]):
                for f in inf["fields"]:
                    self.assertEqual(got.values.get(f), want.get(f), f)


class TestExport(unittest.TestCase):
    def test_exports_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_cli("export", str(ECOM),
                                   "-p", "generic-item-block",
                                   "-o", str(Path(d) / "e.md"))
            self.assertEqual(code, 0, err)
            self.assertTrue((Path(d) / "e.md").exists())

    def test_unsupported_format_is_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_cli("export", str(ECOM), "-f", "pdf",
                                   "-o", str(Path(d) / "e.pdf"))
            self.assertEqual(code, 2)
            self.assertIn("pdf", err.lower())


class TestCardsAndDoctor(unittest.TestCase):
    def test_cards_list_names_a_builtin(self):
        code, out, _ = run_cli("cards", "list")
        self.assertEqual(code, 0)
        self.assertIn("generic-item-block", out)

    def test_cards_show_dumps_the_card(self):
        code, out, _ = run_cli("cards", "show", "generic-item-block")
        self.assertEqual(code, 0)
        self.assertIn("anchor", out.lower())

    def test_cards_validate_unknown_is_bad_input(self):
        code, _, err = run_cli("cards", "validate", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_doctor_reports_python_and_cards(self):
        code, out, _ = run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("Python", out)
        self.assertIn("内置卡", out)


class TestEncoding(unittest.TestCase):
    def test_chinese_survives_a_gbk_host_console(self):
        # The real failure mode on this machine: cp936 stdout turns every
        # Chinese field into mojibake or a UnicodeEncodeError.
        old = os.environ.get("PYTHONIOENCODING")
        os.environ["PYTHONIOENCODING"] = "gbk"
        try:
            buf = io.BytesIO()
            wrapper = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
            with contextlib.redirect_stdout(wrapper):
                code = main(["run", "-p", "generic-item-block",
                             "--text-file", str(ECOM)])
            wrapper.flush()
            self.assertEqual(code, 0)
            self.assertTrue(buf.getvalue())
        finally:
            if old is None:
                os.environ.pop("PYTHONIOENCODING", None)
            else:
                os.environ["PYTHONIOENCODING"] = old


if __name__ == "__main__":
    unittest.main()
