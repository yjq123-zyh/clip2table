"""E1 must activate the tab before it copies, and must never claim otherwise.

The bug this locks down: a hidden tab receives Ctrl+A/Ctrl+C and fires every
keydown handler, but Chromium refuses to write the clipboard for it. The old
code sent the keys anyway and the harvest then failed with "the clipboard
didn't change in 5000 ms" - blaming the page for a condition the tool created.
A fake CDP client is enough to check the ordering, and it costs no browser.

The second rule here is about the price of fixing it. `Page.bringToFront` can
restore a minimised browser window and take the OS foreground with it (measured),
so whatever window was in front must get it back - including when the harvest
fails halfway.
"""
from __future__ import annotations

import unittest
from unittest import mock

from clip2table.cards.schema import Card
from clip2table.errors import ChannelError
from clip2table.harvest import _make_visible, harvest_browser


class FakeCDP:
    """Records the CDP calls in order and answers the two queries used."""

    def __init__(self, visibility="hidden", stays_hidden=False, bring_fails=False):
        self.visibility = visibility
        self.stays_hidden = stays_hidden
        self.bring_fails = bring_fails
        self.flips_after = 0        # attempts that leave the tab hidden first
        self.calls: list[str] = []
        self.brought = 0

    def eval(self, expr, await_promise=False):
        self.calls.append("eval")
        if "visibilityState" in expr:
            return "hidden" if self.stays_hidden else self.visibility
        if "document.querySelector" in expr:
            return True        # select_in found its container
        return None

    def call(self, method, params=None):
        self.calls.append(method)
        if method == "Page.bringToFront":
            if self.bring_fails:
                from clip2table.channels.cdp import CDPError
                raise CDPError("Page.bringToFront: not allowed")
            self.brought += 1
            if self.brought > self.flips_after:
                self.visibility = "visible"
        return {}

    def key_combo(self, vk, ctrl=True):
        self.calls.append(f"key:{vk}")

    def close(self):
        self.calls.append("close")


def card(**harvest) -> Card:
    h = {"select_in": "#list", "select_all": False, "timeout_ms": 50}
    h.update(harvest)
    return Card(name="t", schema=1, channel="browser", harvest=h,
                anchor=r"^(?!\s).+$", anchor_next=r"^\d{4}-\d{2}-\d{2}$")


class TestBringToFrontIsOrderedBeforeTheCopy(unittest.TestCase):
    def test_hidden_tab_is_activated_before_any_key_is_sent(self):
        fake = FakeCDP(visibility="hidden")
        _make_visible(fake, [])
        self.assertEqual(fake.brought, 1)
        self.assertIn("Page.bringToFront", fake.calls)

    def test_hidden_tab_reports_that_the_foreground_may_have_moved(self):
        """The return value is what tells the caller to hand the focus back."""
        self.assertTrue(_make_visible(FakeCDP(visibility="hidden"), []))

    def test_visible_tab_is_not_disturbed(self):
        fake = FakeCDP(visibility="visible")
        notes: list[str] = []
        _make_visible(fake, notes)
        self.assertEqual(fake.brought, 0, "don't reorder tabs for no reason")
        self.assertEqual(notes, [])
        self.assertFalse(_make_visible(fake, []), "nothing was borrowed")

    def test_failure_to_activate_is_reported_not_swallowed(self):
        fake = FakeCDP(visibility="hidden", bring_fails=True)
        with self.assertRaises(ChannelError) as cm:
            _make_visible(fake, [])
        self.assertIn("切不到可见状态", cm.exception.message)
        self.assertIn("最小化", cm.exception.hint or "")

    def test_still_hidden_afterwards_stops_before_any_key(self):
        fake = FakeCDP(visibility="hidden", stays_hidden=True)
        with mock.patch("clip2table.harvest._VISIBLE_BUDGET", 0.05), \
                mock.patch("clip2table.harvest._VISIBLE_POLL", 0.01):
            with self.assertRaises(ChannelError) as cm:
                _make_visible(fake, [])
        self.assertGreaterEqual(fake.brought, 1, "it should have tried")
        self.assertIn("仍是隐藏状态", cm.exception.message)
        self.assertIn("还原", cm.exception.hint or "")

    def test_a_flaky_flip_is_retried_rather_than_reported_as_failure(self):
        """From an identical state bringToFront sometimes does not flip the tab."""
        fake = FakeCDP(visibility="hidden")
        fake.flips_after = 2          # first two attempts leave it hidden
        with mock.patch("clip2table.harvest._VISIBLE_BUDGET", 0.05), \
                mock.patch("clip2table.harvest._VISIBLE_POLL", 0.01):
            self.assertTrue(_make_visible(fake, []))
        self.assertEqual(fake.brought, 3)


class TestBorrowedForegroundIsGivenBack(unittest.TestCase):
    """bringToFront may raise a minimised window and keep the focus; give it back."""

    def test_focus_is_restored_after_a_successful_copy(self):
        fake = FakeCDP(visibility="hidden")
        with mock.patch("clip2table.harvest.kb.foreground",
                        side_effect=[4242, 999, 4242]), \
                mock.patch("clip2table.harvest.kb.restore_focus",
                           return_value=True) as restore:
            try:
                harvest_browser(card(), client=fake, target_url="http://x/")
            except Exception:
                pass
        restore.assert_called_once_with(4242)

    def test_focus_is_restored_even_when_the_harvest_fails(self):
        fake = FakeCDP(visibility="hidden")
        with mock.patch("clip2table.harvest.kb.foreground",
                        side_effect=[4242, 999, 999]), \
                mock.patch("clip2table.harvest.kb.restore_focus",
                           return_value=True) as restore:
            with self.assertRaises(Exception):
                harvest_browser(card(), client=fake, target_url="http://x/")
        restore.assert_called_once_with(4242)

    def test_nothing_is_restored_when_the_foreground_never_moved(self):
        fake = FakeCDP(visibility="hidden")
        with mock.patch("clip2table.harvest.kb.foreground", return_value=4242), \
                mock.patch("clip2table.harvest.kb.restore_focus") as restore:
            try:
                harvest_browser(card(), client=fake, target_url="http://x/")
            except Exception:
                pass
        restore.assert_not_called()

    def test_failed_restore_is_said_out_loud(self):
        """A restore that did not take must not be reported as one that did."""
        from clip2table.harvest import _give_back_foreground

        notes: list[str] = []
        with mock.patch("clip2table.harvest.kb.foreground", return_value=999), \
                mock.patch("clip2table.harvest.kb.restore_focus",
                           return_value=False):
            _give_back_foreground(4242, notes)
        self.assertTrue(any("没能把焦点还回去" in n for n in notes), notes)

    def test_successful_restore_is_reported_truthfully(self):
        from clip2table.harvest import _give_back_foreground

        notes: list[str] = []
        with mock.patch("clip2table.harvest.kb.foreground",
                        side_effect=[999, 4242]), \
                mock.patch("clip2table.harvest.kb.restore_focus",
                           return_value=True):
            _give_back_foreground(4242, notes)
        self.assertTrue(any("已把焦点还给原来的窗口" in n for n in notes), notes)


class TestHarvestSendsNoKeysBeforeActivation(unittest.TestCase):
    def test_bring_to_front_precedes_key_a_and_key_c(self):
        fake = FakeCDP(visibility="hidden")
        # Not enough of a page to parse, but the ordering is what is under test.
        try:
            harvest_browser(card(), client=fake, target_url="http://x/")
        except Exception:
            pass  # clipboard is not the subject here
        i = fake.calls.index("Page.bringToFront")
        keys = [n for n, c in enumerate(fake.calls) if c.startswith("key:")]
        self.assertTrue(keys, "the keys must still be sent")
        self.assertLess(i, min(keys),
                        "the tab must be visible BEFORE the first keystroke")

    def test_activate_tab_can_be_disabled_for_tests(self):
        fake = FakeCDP(visibility="hidden")
        try:
            harvest_browser(card(), client=fake, target_url="http://x/",
                            activate_tab=False)
        except Exception:
            pass
        self.assertNotIn("Page.bringToFront", fake.calls)


if __name__ == "__main__":
    unittest.main()
