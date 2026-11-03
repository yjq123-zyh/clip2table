"""Shared collection lifecycle and navigation recovery."""
import unittest
from unittest.mock import Mock, patch

from clip2table import harvest as hv, ladder
from clip2table.cards.schema import Card
from clip2table.channels import session
from clip2table.errors import ChannelError
from clip2table.models import Captured


class Client:
    def __init__(self):
        self.url = "https://example.test/orders?p=9"
        self.close = Mock()
        self.navigations = []

    def call(self, method, params):
        self.url = params["url"]
        self.navigations.append(self.url)

    def eval(self, expression):
        return self.url if expression == "location.href" else "complete"


def card():
    return Card(name="orders", channel="browser", segment_mode="lines",
                pause_ms=0, paging_url_template="https://example.test/orders?p={page}")


def read(card, page=1, **kwargs):
    url = kwargs["client"].url
    return hv.Round(Captured(kind="text", payload=f"order-{page}",
                             observed_url=url, source_url=url, channel="browser"), page=page)


class TestSession(unittest.TestCase):
    def test_single_and_paged_reads_share_navigation_and_close(self):
        client = Client()
        with patch.object(session.cdp_mod, "connect", return_value=(client, {})), \
                patch.object(hv, "harvest_browser", side_effect=read):
            one = hv.harvest(card(), page=3)
            many = hv.harvest_pages(card(), max_pages=2, start_page=3)
        self.assertEqual(one.captured.provenance(), many.first.captured.provenance())
        self.assertEqual(client.navigations, [
            "https://example.test/orders?p=3", "https://example.test/orders?p=3",
            "https://example.test/orders?p=4"])
        self.assertEqual(client.close.call_count, 2)

    def test_open_failure_closes_connection(self):
        client = Client()
        with patch.object(session.cdp_mod, "connect", return_value=(client, {})), \
                patch.object(session, "navigate", side_effect=ChannelError("timeout")):
            with self.assertRaises(ChannelError):
                hv.harvest(card())
        client.close.assert_called_once()

    def test_commit_cancellation_closes_connection(self):
        client = Client()
        with patch.object(session.cdp_mod, "connect", return_value=(client, {})), \
                patch.object(hv, "harvest_browser", side_effect=read):
            with self.assertRaises(KeyboardInterrupt):
                hv.harvest_pages(card(), on_page=Mock(side_effect=KeyboardInterrupt))
        client.close.assert_called_once()

    def test_close_is_idempotent(self):
        client = Client()
        with patch.object(session.cdp_mod, "connect", return_value=(client, {})):
            with session.Session(card()) as current:
                current.close()
        client.close.assert_called_once()

    def test_ladder_uses_shared_browser_navigation(self):
        client = Client()
        with patch.object(session.cdp_mod, "connect", return_value=(client, {})), \
                patch.object(hv, "harvest_browser", side_effect=read):
            captured, reason = ladder.try_browser("https://example.test/orders", port=9222)
        self.assertEqual(reason, "")
        self.assertEqual(captured.observed_url, "https://example.test/orders")
        client.close.assert_called_once()

    def test_ladder_http_preserves_its_timeout(self):
        with patch.object(hv, "harvest_http", return_value=hv.Round(
                Captured(kind="text", payload="order"))) as fetch:
            captured, reason = ladder.try_http("https://example.test/orders", timeout=2)
        self.assertEqual(captured.payload, "order")
        self.assertEqual(reason, "")
        self.assertEqual(fetch.call_args.kwargs["timeout"], 2)

    def test_desktop_advance_restores_focus_after_key_failure(self):
        task = Card(name="desktop", channel="desktop", paging_style="key")
        with patch.object(hv, "_desktop_target_hwnd", return_value=5), \
                patch.object(session.kb, "foreground", return_value=3), \
                patch.object(session.kb, "activate", return_value="ok"), \
                patch.object(session.kb, "require_foreground"), \
                patch.object(hv, "send_named_key", side_effect=ChannelError("key failed")), \
                patch.object(hv, "_give_back_foreground") as restore:
            with self.assertRaises(ChannelError):
                session.advance(task, None, False)
        restore.assert_called_once_with(3, [])


class TestNavigation(unittest.TestCase):
    def test_matching_url_without_ready_document_times_out(self):
        client = Client()
        client.eval = lambda expr: client.url if expr == "location.href" else "loading"
        with patch.object(session.time, "monotonic", side_effect=[0, 0, 1]), \
                patch.object(session.time, "sleep"):
            with self.assertRaises(ChannelError) as caught:
                session.navigate(client, "https://example.test/orders", 10)
        self.assertEqual(caught.exception.code, "navigation_timeout")

    def test_navigation_retries_replaced_execution_context(self):
        client = Client()
        client.eval = Mock(side_effect=[session.cdp_mod.CDPError("context replaced"),
                                       "https://example.test/orders", "complete"])
        with patch.object(session.time, "sleep"):
            self.assertEqual(session.navigate(client, "https://example.test/orders", 100),
                             "https://example.test/orders")


if __name__ == "__main__":
    unittest.main()
