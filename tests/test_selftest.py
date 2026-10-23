"""selftest: the docs and the real argument parser must not disagree.

The skill file is the thing an agent reads instead of `--help`. If it names a
subcommand that does not exist, the agent will run it, get a usage error, and
usually conclude the tool is broken rather than the document is stale. The same
is true of a usage doc that tells a human to run a command that was removed.

Three things are pinned here:
  - the shipped docs pass as-is;
  - a doc naming a bogus subcommand makes the command exit non-zero;
  - the check covers docs/ too, not only SKILL.md (it used to miss them, which
    is how `clip2table cards test` -- a command that never existed -- survived
    in usage.md).

The failure cases are the whole point. A check that can only ever pass is not a
check, it is decoration.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from clip2table import cli
from clip2table.errors import BAD_INPUT, OK

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
DOCS = [ROOT / "README.md", ROOT / "docs" / "usage.md",
        ROOT / "docs" / "architecture.md", ROOT / "docs" / "design-notes.md"]


class _Args:
    def __init__(self) -> None:
        self.cmd = "selftest"


def run_selftest() -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.cmd_selftest(_Args())
    return code, buf.getvalue()


class TestShippedDocs(unittest.TestCase):
    def test_skill_file_exists(self):
        self.assertTrue(SKILL.is_file(), "SKILL.md 不在仓库根，selftest 会静默跳过对账")

    def test_every_doc_is_actually_scanned(self):
        """A doc that is not in the scan list cannot be checked, and we would
        not notice -- the check would still print a cheerful ✓."""
        scanned = {p.name for p in cli._selftest_doc_paths() if p.exists()}
        for path in DOCS:
            with self.subTest(doc=path.name):
                self.assertIn(path.name, scanned)

    def test_shipped_docs_pass(self):
        code, out = run_selftest()
        self.assertEqual(code, OK, out)
        self.assertIn("全部存在", out)
        self.assertIn("usage.md", out)

    def test_skill_mentions_every_command_an_agent_needs(self):
        """Not required by selftest, but a gap here means the doc is useless."""
        text = SKILL.read_text(encoding="utf-8")
        for sub in ("run", "harvest", "infer", "doctor", "test"):
            with self.subTest(sub=sub):
                self.assertIn(f"clip2table {sub}", text)


class TestNothingToCheckIsAFailure(unittest.TestCase):
    """A check that skips itself when it cannot see its inputs is not a check.

    This is the exact bug `selftest` exists to catch, and it had it too:
    `clip2table selftest` from a wheel install printed "没找到任何文档，跳过对账"
    and exited 0. A wheel ships code, not docs, so the installed copy could
    never see the files -- and reported success for having verified nothing.
    """

    def test_zero_docs_is_a_failure_not_a_pass(self):
        orig = cli._selftest_doc_paths
        cli._selftest_doc_paths = lambda: []
        try:
            code, out = run_selftest()
        finally:
            cli._selftest_doc_paths = orig
        self.assertEqual(code, BAD_INPUT, out)
        self.assertNotIn("跳过对账", out)
        self.assertIn("什么也没验证", out)

    def test_docs_that_are_all_missing_are_a_failure(self):
        """The installed-copy case: paths resolve, nothing is there."""
        tmp = Path(tempfile.mkdtemp(prefix="c2t-nodocs-"))
        try:
            orig = cli._selftest_doc_paths
            cli._selftest_doc_paths = lambda: [tmp / "SKILL.md", tmp / "usage.md"]
            try:
                code, out = run_selftest()
            finally:
                cli._selftest_doc_paths = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(code, BAD_INPUT, out)
        self.assertIn("SKILL.md", out)   # says what it looked for


class TestBrokenDocIsRejected(unittest.TestCase):
    def test_a_bogus_subcommand_fails_the_check(self):
        code, out = run_selftest_with_skill(
            "先跑 `clip2table frobnicate` 检查环境。\n")
        self.assertEqual(code, BAD_INPUT)
        self.assertIn("frobnicate", out)
        self.assertIn("不存在的子命令", out)

    def test_one_bad_name_among_good_ones_still_fails(self):
        code, out = run_selftest_with_skill(
            "`clip2table doctor` 和 `clip2table nope-not-real` 都要能用。\n")
        self.assertEqual(code, BAD_INPUT)
        self.assertIn("nope-not-real", out)

    def test_a_bogus_command_in_a_docs_file_fails_too(self):
        """This is the regression for `clip2table cards test` in usage.md."""
        code, out = run_selftest_with_skill(
            "`clip2table doctor` 没问题。\n", extra={"usage.md": "跑 `clip2table cards test x.toml`。\n"})
        self.assertEqual(code, BAD_INPUT)
        self.assertIn("usage.md", out)
        self.assertIn("test", out)

    def test_frontmatter_does_not_leak_into_the_command_list(self):
        """'name: clip2table\\n description:' used to be read as 'clip2table description'."""
        code, out = run_selftest_with_skill(
            "---\nname: clip2table\ndescription: 一个工具。\n---\n\n"
            "用 `clip2table doctor` 自检。\n")
        self.assertEqual(code, OK, out)
        self.assertNotIn("description", out)


def run_selftest_with_skill(text: str, extra: dict[str, str] | None = None) -> tuple[int, str]:
    """Run cmd_selftest against `text` (and optional extra docs) instead of the repo's."""
    tmp = Path(tempfile.mkdtemp(prefix="c2t-skill-"))
    try:
        fake = tmp / "SKILL.md"
        fake.write_text(text, encoding="utf-8")
        paths = [fake]
        for name, body in (extra or {}).items():
            p = tmp / name
            p.write_text(body, encoding="utf-8")
            paths.append(p)
        orig = cli._selftest_doc_paths
        cli._selftest_doc_paths = lambda: paths
        try:
            return run_selftest()
        finally:
            cli._selftest_doc_paths = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
