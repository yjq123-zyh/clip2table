"""Channel connection, navigation and recovery shared by collection entry points."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from ..cards.schema import Card
from ..errors import ChannelError, InputError
from . import cdp as cdp_mod, keyboard as kb


def navigate(client, url: str, timeout_ms: int) -> str:
    """Navigate and wait for the resulting document."""
    client.call("Page.navigate", {"url": url})
    return wait_ready(client, timeout_ms, url)


def wait_ready(client, timeout_ms: int, url: str = "") -> str:
    """Wait for a complete document and return its observed address."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            seen = str(client.eval("location.href") or "")
            ready = str(client.eval("document.readyState") or "")
            if seen and ready == "complete":
                return seen
        except cdp_mod.CDPError:
            pass  # Navigation replaces the execution context.
        time.sleep(0.05)
    raise ChannelError("等待页面就绪超时", code="navigation_timeout",
                       details={"url": url, "timeout_ms": timeout_ms})


class Session:
    """Own a channel connection for one read or a sequence of pages."""

    def __init__(self, card: Card, *, port=None, allow_force=False,
                 page=1, positioned=False, url="", timeout=8.0):
        if card.channel not in ("http", "browser", "desktop"):
            raise InputError(f"卡 {card.name!r} 需要指定采集通道")
        self.card = card
        self.port = port
        self.allow_force = allow_force
        self.page = page
        self.positioned = positioned
        self.url = url
        self.timeout = timeout
        self.client = None
        self.target = {}
        self.target_url = ""

    @property
    def capabilities(self):
        return {"formats": ("text", "html", "json") if self.card.channel == "http" else ("text", "html"),
                "restore": can_restore(self.card)[0],
                "channel": self.card.channel}

    def __enter__(self):
        from .. import harvest as hv
        try:
            if self.card.channel == "browser":
                self.client, self.target = cdp_mod.connect(self.port, hv._url_hint(self.card))
                self.target_url = str(self.target.get("url", ""))
                if self.url:
                    self.target_url = navigate(
                        self.client, self.url,
                        int((self.card.harvest or {}).get("timeout_ms", 5000)))
                elif not self.positioned and (self.page > 1 or self.card.paging_url_template):
                    self.restore(self.page)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *exc):
        self.close()

    def close(self):
        client, self.client = self.client, None
        if client is not None:
            client.close()

    def read(self, page=None):
        from .. import harvest as hv
        self.page = self.page if page is None else page
        if self.card.channel == "browser":
            wait_ready(self.client, int((self.card.harvest or {}).get("timeout_ms", 5000)))
            rnd = hv.harvest_browser(self.card, page=self.page, client=self.client,
                                     target_url=self.target_url)
            self.target_url = rnd.captured.observed_url or rnd.captured.source_url or self.target_url
            for key, source in (("tab_id", "id"), ("target_title", "title")):
                if self.target.get(source):
                    rnd.captured.meta[key] = self.target[source]
            return rnd
        if self.card.channel == "http":
            return hv.harvest_http(self.card, page=self.page, timeout=self.timeout)
        return hv.harvest_desktop(self.card, page=self.page, allow_force=self.allow_force)

    def restore(self, page):
        from .. import harvest as hv
        if self.card.channel == "browser":
            self.target_url = hv.restore_to(self.client, self.card, page)
        elif not can_restore(self.card)[0]:
            raise ChannelError("请确认继续采集的位置", reason="resume_position_confirmation")
        self.page = page
        return {"page": page, "url": self.target_url}

    def advance(self, page):
        from .. import harvest as hv
        if self.card.channel == "browser" and self.card.paging_url_template:
            self.restore(page + 1)
            return True
        moved = hv._advance(self.card, self.client, self.allow_force)
        if moved:
            self.page = page + 1
        return moved


def can_restore(card: Card) -> tuple[bool, str]:
    """Return the task's positioning capability and explanation."""
    if card.channel == "http":
        if "{page}" in str((card.fetch or {}).get("url") or ""):
            return True, "直连卡有 {page} 占位符，第 N 页就是一次 GET"
        return False, "直连卡的 [fetch].url 里没有 {page} 占位符，只能取一页"
    if card.channel == "browser":
        if card.paging_url_template:
            return True, "浏览器卡有 url_template，可以直接导航到第 N 页"
        if card.paging_style == "click":
            return False, ("浏览器卡的列表地址不随页码变化，翻页只能点「下一页」；"
                           "没有 url_template 就无法确认标签页现在停在第几页")
        return False, "浏览器卡没有 [paging].url_template，无法导航到指定页"
    return False, f"{card.channel} 通道读不到当前界面位置，无法自动恢复"


def restore_to(client: cdp_mod.CDP, card: Card, page: int,
               notes: list[str] | None = None) -> str:
    """Navigate to an addressable page and verify its URL."""
    notes = notes if notes is not None else []
    tmpl = card.paging_url_template
    if not tmpl:
        raise InputError(
            f"卡 {card.name!r} 没有 [paging].url_template，无法恢复到第 {page} 页",
            hint="在卡里加 url_template = \"https://.../list?page={page}\"，"
                 "或者用 --append-from-here 从当前屏幕接着采")
    want = tmpl.replace("{page}", str(page))
    seen = navigate(client, want, int((card.harvest or {}).get("timeout_ms", 20000)))
    # Same page means same path+query; the fragment is not part of the address
    # of a page in a list, so a hash difference must not fail the check.
    want_key = urlsplit(want)
    got_key = urlsplit(seen)
    if (want_key.scheme, want_key.netloc, want_key.path, want_key.query) != \
            (got_key.scheme, got_key.netloc, got_key.path, got_key.query):
        raise ChannelError(
            f"想恢复到第 {page} 页，导航到了 {seen[:80]}，不是 {want[:80]}",
            hint="站点可能重定向了；用 --append-from-here 明确从当前屏幕接着采",
            reason="resume_position_confirmation")
    notes.append(f"已导航到第 {page} 页：{want[:80]}")
    return seen


def advance(card: Card, client: cdp_mod.CDP | None,
             allow_force: bool) -> bool:
    """Move to the next page. False means the list is over."""
    from .. import harvest as hv

    if card.channel == "http":
        return "{page}" in str((card.fetch or {}).get("url") or "")

    if card.channel == "browser":
        if client is None:
            return False
        if card.paging_style == "click" and card.paging_next:
            return hv.click_next_page(client, card.paging_next)
        if card.paging_style == "key":
            client.key_simple(cdp_mod.VK_PAGEDOWN)
            return True
        return False

    # desktop: page key inside the same window
    if card.paging_style in ("key", "none"):
        hwnd = hv._desktop_target_hwnd(card, allow_force)
        previous = kb.foreground()
        try:
            if kb.activate(hwnd) == "none":
                return False
            kb.require_foreground(hwnd)
            hv.send_named_key(str((card.target or {}).get("next_key", "pagedown")))
            return True
        finally:
            hv._give_back_foreground(previous, [])
    return False
