"""The focus rules that the desktop channel depends on.

These test the decision logic, not Win32 itself: which control the module is
willing to focus, and when it refuses to guess. The measured incident behind them
is in `keyboard.focus_text_control`'s docstring: Windows 11 Notepad hosts six
RichEdit controls in one window (one per tab), only the active tab's is visible,
and focusing a hidden sibling changes hwndFocus while Ctrl+C still copies nothing.
"""

import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip2table.channels import keyboard as kb  # noqa: E402


class TestFocusTextControl(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(kb, "_focus_owner", return_value=(0, 0)),
            mock.patch.object(kb, "_child_text_controls", return_value=[]),
            mock.patch.object(kb, "_class_name", side_effect=lambda h: {
                111: "RichEditD2DPT", 222: "RichEditD2DPT",
                333: "InputSiteWindowClass",
            }.get(h, "Other")),
            mock.patch.object(kb.user32, "IsWindowVisible",
                              side_effect=lambda h: ctypes.wintypes.HWND(
                                  h.value if hasattr(h, "value") else h) is None),
        ]
        started = [p.start() for p in self._patches]
        self.focus_owner, self.children, self.class_of, self.visible = started

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _visible_set(self, hwnds):
        self.visible.side_effect = (
            lambda h: int(h.value if hasattr(h, "value") else h) in hwnds)

    def test_already_on_a_visible_text_control_is_left_alone(self):
        self.focus_owner.return_value = (111, 0)
        self._visible_set({111})
        with mock.patch.object(kb.user32, "SetFocus") as set_focus:
            self.assertEqual(kb.focus_text_control(1000), "")
        set_focus.assert_not_called()

    def test_focus_on_a_hidden_sibling_is_replaced(self):
        # The exact measured failure: focus sits on a RichEdit that belongs to
        # another tab, so it is a text control but not the one on screen.
        self.focus_owner.return_value = (222, 0)
        self._visible_set({111})
        self.children.return_value = [111, 222]
        with mock.patch.object(kb.user32, "SetFocus") as set_focus, \
                mock.patch.object(kb.user32, "AttachThreadInput",
                                  return_value=True), \
                mock.patch.object(kb.user32, "GetWindowThreadProcessId",
                                  return_value=7), \
                mock.patch.object(kb.kernel32, "GetCurrentThreadId",
                                  return_value=8):
            kb.focus_text_control(1000)
        set_focus.assert_called_once()
        self.assertEqual(set_focus.call_args[0][0].value, 111)

    def test_focus_on_an_input_site_is_replaced(self):
        self.focus_owner.return_value = (333, 0)      # InputSiteWindowClass
        self._visible_set({111})
        self.children.return_value = [111]
        with mock.patch.object(kb.user32, "SetFocus") as set_focus, \
                mock.patch.object(kb.user32, "AttachThreadInput",
                                  return_value=True), \
                mock.patch.object(kb.user32, "GetWindowThreadProcessId",
                                  return_value=7), \
                mock.patch.object(kb.kernel32, "GetCurrentThreadId",
                                  return_value=8):
            kb.focus_text_control(1000)
        set_focus.assert_called_once()

    def test_two_visible_text_controls_is_refused(self):
        # Cannot tell which document the user is looking at; guessing would put
        # Ctrl+A into the wrong one.
        self.children.return_value = [111, 222]
        self._visible_set({111, 222})
        with mock.patch.object(kb.user32, "SetFocus") as set_focus, \
                mock.patch.object(kb.user32, "AttachThreadInput",
                                  return_value=True), \
                mock.patch.object(kb.user32, "GetWindowThreadProcessId",
                                  return_value=7), \
                mock.patch.object(kb.kernel32, "GetCurrentThreadId",
                                  return_value=8):
            self.assertEqual(kb.focus_text_control(1000), "")
        set_focus.assert_not_called()

    def test_no_text_control_is_not_an_error(self):
        # A canvas or grid program has nothing to focus; that is not a defect.
        self.children.return_value = []
        with mock.patch.object(kb.user32, "SetFocus") as set_focus:
            self.assertEqual(kb.focus_text_control(1000), "")
        set_focus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
