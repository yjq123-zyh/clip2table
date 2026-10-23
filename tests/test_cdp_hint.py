"""The doctor's paste-ready browser command.

Three defects are pinned here, each found by running the hint verbatim:

1. `msedge` / `chrome` are not on PATH on a stock Windows install, so a hint
   written that way answers "not recognized as an internal or external
   command". The real absolute path has to be in the line.
2. `& "<exe>" --flags` blocks the shell until the browser exits, which reads
   as a hang. `Start-Process` returns immediately.
3. A profile directory that a leftover instance already owns makes the new
   launch hand its flags to that instance, which ignores
   --remote-debugging-port: the directory gets created, the port never opens,
   and nothing explains why. The profile name therefore carries the port.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip2table.channels import cdp  # noqa: E402


class TestFindBrowser(unittest.TestCase):
    def test_returns_label_and_absolute_exe(self):
        found = cdp.find_browser()
        if found is None:
            self.skipTest("no Edge or Chrome installed on this machine")
        label, exe = found
        self.assertIn(label, ("Edge", "Chrome"))
        self.assertTrue(Path(exe).is_file())
        self.assertTrue(exe.lower().endswith(".exe"))

    def test_none_when_nothing_installed(self):
        with mock.patch.object(cdp, "_BROWSER_CANDIDATES", ()):
            self.assertIsNone(cdp.find_browser())


class TestHintForPort(unittest.TestCase):
    def test_hint_carries_an_absolute_path_not_a_bare_name(self):
        hint = cdp.hint_for_port(9222)
        self.assertNotIn("  msedge   --", hint)  # the old, unusable shape
        found = cdp.find_browser()
        if found is None:
            self.skipTest("no browser installed")
        self.assertIn(found[1], hint)
        self.assertRegex(hint, r'[A-Za-z]:\\')  # a real drive path

    def test_hint_does_not_block_the_shell(self):
        hint = cdp.hint_for_port(9222)
        self.assertIn("Start-Process", hint)

    def test_profile_carries_the_port(self):
        # Without this, a leftover instance owning the same profile swallows
        # --remote-debugging-port and the port silently never opens.
        self.assertIn("c2t-profile-9222", cdp.hint_for_port(9222))
        self.assertIn("c2t-profile-9333", cdp.hint_for_port(9333))

    def test_profile_variable_gets_expanded(self):
        # Single quotes would leave the browser creating a directory literally
        # named "$env:TEMP\c2t-profile-9222". The argument must be the
        # double-quoted "-user-data-dir=<var>" form.
        hint = cdp.hint_for_port(9222)
        self.assertIn('"--user-data-dir=$env:TEMP\\c2t-profile-9222"', hint)

    def test_port_appears_in_the_debugging_flag(self):
        self.assertIn("--remote-debugging-port=9222", cdp.hint_for_port(9222))

    def test_fallback_shape_when_no_browser_found(self):
        with mock.patch.object(cdp, "find_browser", return_value=None):
            hint = cdp.hint_for_port(9222)
        self.assertIn("没在常见安装位置找到", hint)
        self.assertIn("<你的浏览器路径>", hint)
        self.assertIn("--remote-debugging-port=9222", hint)


if __name__ == "__main__":
    unittest.main()
