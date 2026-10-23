"""Resuming a browser harvest has to MOVE the tab, not just increment a number.

The bug these tests guard: `resume_plan` said "browser can resume" for every
browser card, but `harvest_browser` copied whatever the tab was showing. A
dataset claiming to continue at page 4 could be filled with page 1's rows while
every counter agreed it had worked.
"""
import unittest

from clip2table.cards.schema import Card
from clip2table.dataset import Dataset
from clip2table.errors import ChannelError, InputError
from clip2table.harvest import can_restore, restore_to
import tempfile
from pathlib import Path


def _card(**kw):
    base = dict(name="t", schema=1, channel="browser", fields=[])
    base.update(kw)
    return Card(**base)


class FakeClient:
    """Records navigation and reports a configurable address bar."""

    def __init__(self, lands_on=None, ready="complete", dead_after_nav=False):
        self.nav = []
        self.lands_on = lands_on
        self.ready = ready
        self.dead = dead_after_nav

    def call(self, method, params=None):
        if method == "Page.navigate":
            self.nav.append((params or {}).get("url"))
        return {}

    def eval(self, expr, await_promise=False):
        if self.dead:
            raise RuntimeError("execution context destroyed")
        if expr == "location.href":
            return self.lands_on if self.lands_on is not None else (
                self.nav[-1] if self.nav else "")
        if expr == "document.readyState":
            return self.ready
        return ""


class TestCanRestore(unittest.TestCase):
    def test_browser_with_a_page_url_can_resume(self):
        ok, why = can_restore(_card(paging_url_template="https://x/list?p={page}"))
        self.assertTrue(ok, why)

    def test_browser_without_one_cannot(self):
        ok, why = can_restore(_card(paging_style="click", paging_next="#next"))
        self.assertFalse(ok)
        self.assertIn("url_template", why)

    def test_http_needs_a_page_placeholder(self):
        self.assertTrue(can_restore(_card(channel="http",
                                          fetch={"url": "u?p={page}"}))[0])
        self.assertFalse(can_restore(_card(channel="http",
                                           fetch={"url": "u"}))[0])

    def test_desktop_never_resumes(self):
        self.assertFalse(can_restore(_card(channel="desktop"))[0])


class TestRestoreTo(unittest.TestCase):
    def test_it_navigates_and_verifies(self):
        c = _card(paging_url_template="https://x/list?p={page}")
        cl = FakeClient()
        notes = []
        got = restore_to(cl, c, 3, notes)
        self.assertEqual(cl.nav, ["https://x/list?p=3"])
        self.assertEqual(got, "https://x/list?p=3")
        self.assertTrue(any("第 3 页" in n for n in notes))

    def test_a_redirect_is_an_error_not_a_silent_continue(self):
        c = _card(paging_url_template="https://x/list?p={page}")
        cl = FakeClient(lands_on="https://x/login")
        with self.assertRaises(ChannelError):
            restore_to(cl, c, 4)

    def test_a_hash_difference_does_not_fail_the_check(self):
        c = _card(paging_url_template="https://x/list?p={page}")
        cl = FakeClient(lands_on="https://x/list?p=5#top")
        self.assertTrue(restore_to(cl, c, 5))

    def test_no_template_is_a_clear_refusal(self):
        with self.assertRaises(InputError) as ctx:
            restore_to(FakeClient(), _card(), 2)
        self.assertIn("append-from-here", str(ctx.exception.hint))


class TestResumePlanUsesTheCard(unittest.TestCase):
    def _ds(self, tmp):
        ds = Dataset(tmp, fields=["id"], keys=["id"])
        ds.open()
        ds.commit_page(1, "one", {"channel": "browser"},
                       __import__("clip2table.dataset", fromlist=["ParseResult"])
                       .ParseResult(fields=["id"], rows=[
                           __import__("clip2table.models", fromlist=["Row"])
                           .Row(values={"id": "1"})]))
        return ds

    def test_a_click_only_browser_card_is_not_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = self._ds(tmp)
            ok, why = can_restore(_card(paging_style="click", paging_next="#n"))
            plan = ds.resume_plan("browser", restorable=ok, why_not=why)
            self.assertFalse(plan["resume"])
            self.assertTrue(plan["needs_explicit_append"])
            self.assertIn("url_template", plan["why"])

    def test_a_url_addressable_browser_card_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = self._ds(tmp)
            ok, why = can_restore(_card(paging_url_template="https://x?p={page}"))
            plan = ds.resume_plan("browser", restorable=ok, why_not=why)
            self.assertTrue(plan["resume"])
            self.assertEqual(plan["next_page"], 2)


if __name__ == "__main__":
    unittest.main()


class TestHarvestPagesRestoresFirst(unittest.TestCase):
    """The loop must move the tab BEFORE it copies anything."""

    def test_restore_runs_before_the_first_capture(self):
        import clip2table.harvest as hv
        from clip2table.models import Captured, ParseResult, Row
        from clip2table.cards.schema import Card

        order = []

        class Cl:
            def eval(self, expr, await_promise=False):
                return "http://x/list?p=4"

            def close(self):
                pass

        def fake_restore(client, card, page, notes=None):
            order.append(("restore", page))
            return "http://x/list?p=4"

        def fake_browser(card, page=1, port=None, client=None,
                         target_url="", activate_tab=True):
            order.append(("capture", page))
            cap = Captured(kind="text", payload="a\nb", channel="browser",
                           engine="E1", source_url="http://x/list?p=4")
            return hv.Round(captured=cap, page=page, engine="E1",
                            channel="browser", why="stub",
                            result=ParseResult(fields=["t"], rows=[
                                Row(values={"t": "a"}), Row(values={"t": "b"})]))

        card = Card(name="c", schema=1, channel="browser", fields=[],
                    paging_url_template="http://x/list?p={page}")
        old_restore, old_browser = hv.restore_to, hv.harvest_browser
        hv.restore_to, hv.harvest_browser = fake_restore, fake_browser
        try:
            old_connect = hv.cdp_mod.connect
            hv.cdp_mod.connect = lambda port, hint=None: (Cl(), {"url": "http://x"})
            try:
                hv.harvest_pages(card, max_pages=1, start_page=4)
            finally:
                hv.cdp_mod.connect = old_connect
        finally:
            hv.restore_to, hv.harvest_browser = old_restore, old_browser

        self.assertEqual(order[0], ("restore", 4))
        self.assertEqual(order[1][0], "capture")
        self.assertEqual(order[1][1], 4)
