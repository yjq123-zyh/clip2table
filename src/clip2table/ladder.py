"""The channel ladder: A -> B -> C, decided by evidence, not by preference.

Why a ladder at all: the cheapest channel that actually works should win, and the
user should not have to know which one that is. Direct HTTP needs nothing running
and answers in milliseconds; a real browser gets past login walls and JS shells but
needs a browser with a debug port; the desktop channel reaches anything on the
screen but takes over the foreground and presses OS-level keys.

The order is not "try the good one, fall back to the bad one". Each rung has a
reason to fail that is *specific and reportable*, and the report's first line says
which rung answered and why -- because "it worked" is not the same as "it worked
the way you think it did". A user who believes they are hitting an API and is
actually driving their browser is going to have a bad time.

Rung C deliberately comes last even though it is the broadest: it steals the
foreground and presses real keys. It is the right answer for a desktop app with no
other way in, and the wrong answer for a plain HTML page.
"""
from __future__ import annotations

import re
import time

from .cards.schema import Card
from .channels import cdp as cdp_mod
from .channels import http as http_mod
from .errors import ChannelError, InputError
from .models import Captured

# A must not hang the ladder. Two seconds is short enough that a dead host is
# obviously dead, long enough for a slow-but-alive intranet page to answer.
A_TIMEOUT = 2.0
# A pinned channel is an explicit instruction, so it gets a real timeout rather
# than the ladder's "is it worth trying" timeout.
PINNED_TIMEOUT = 15.0
# After Page.navigate, the document needs a moment before it will accept
# keystrokes. Not a poll: a poll for "ready" would need to know what ready means
# for an unknown site, and guessing wrong turns a slow page into a failed page.
NAV_SETTLE = 0.8

_HOST_RE = re.compile(r"^https?://([^/?#]+)", re.IGNORECASE)


class Decision:
    """Which rung answered, and the evidence for it."""

    def __init__(self, channel: str, engine: str, why: str):
        self.channel = channel
        self.engine = engine
        self.why = why

    def line(self) -> str:
        return f"通道 {self.channel} ｜ 引擎 {self.engine} ｜ 判定依据：{self.why}"

    def __repr__(self) -> str:
        return f"Decision({self.channel!r}, {self.why!r})"


def _host(url: str) -> str:
    m = _HOST_RE.match(url or "")
    return m.group(1).lower() if m else ""


def _scratch_card(url: str) -> Card:
    """A card with no field rules: the ladder only needs to GET the text.

    Cutting it into fields is `infer`'s job, and giving the ladder a fake split
    rule would mean the ladder decides the shape of the data -- exactly the
    coupling the card mechanism exists to remove.
    """
    card = Card(name="ladder-scratch", channel="browser")
    card.segment_mode = "single"
    card.harvest = {"select_all": True, "timeout_ms": 5000,
                    "url_contains": _host(url)}
    return card


# --------------------------------------------------------------------- rung A

def try_http(url: str, timeout: float = A_TIMEOUT
             ) -> tuple[Captured | None, str]:
    """Rung A. Returns (capture, reason_it_failed); exactly one is meaningful."""
    result = http_mod.fetch(url, timeout=timeout)
    if not result.ok:
        return None, result.reason or f"HTTP {result.status}"
    kind = "html" if "<" in result.body[:400] else "text"
    return (Captured(kind=kind, payload=result.body, source_url=result.url,
                     requested_url=url, observed_url=result.url,
                     channel="http", engine="HTTP", formats=["http"]), "")


# --------------------------------------------------------------------- rung B

def try_browser(url: str, port: int | None = None
                ) -> tuple[Captured | None, str]:
    """Rung B. Navigates a real tab, then copies with real keys.

    Navigation goes through `Page.navigate` rather than `location.href = `,
    because a tab that failed to load sits on `chrome-error://` and IGNORES the
    JS assignment: it stays on the error page while reporting success, and the
    harvest then turns "this site can't be reached" into tidy rows.
    """
    found = port or cdp_mod.find_debug_port()
    if found is None:
        return None, "没有浏览器开着调试端口"
    client = None
    try:
        client, _target = cdp_mod.connect(found, url_contains=_host(url))
        client.call("Page.navigate", {"url": url})
        time.sleep(NAV_SETTLE)
        href = str(client.eval("location.href") or "")
        if href.startswith("chrome-error://"):
            return None, f"浏览器也打不开这个地址（{href}）"
        from .harvest import harvest_browser
        rnd = harvest_browser(_scratch_card(url), page=1, client=client,
                              target_url=url)
        return rnd.captured, ""
    except (ChannelError, cdp_mod.CDPError) as e:
        return None, str(e)
    finally:
        if client is not None:
            client.close()


# --------------------------------------------------------------------- rung C

# What counts as "a browser window" for rung C. Deliberately a whitelist: rung C
# presses Ctrl+A and Ctrl+C into whatever window it is told to drive, and doing
# that to an editor, a spreadsheet, or a mail client can destroy work.
_BROWSER_EXES = ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe",
                 "opera.exe", "vivaldi.exe")


def try_desktop(url: str) -> tuple[Captured | None, str]:
    """Rung C. Drive the window the user is already looking at.

    This rung cannot be told a URL. A browser window's title is the page's
    `<title>`, not its address, so "find the window showing this URL" is not
    answerable without a debug port -- which is the thing rung B needed and did
    not have. So the contract is the honest one: the user brings the window to
    the front, and this drives THAT window.

    The cost of that honesty is a real one, so it is guarded rather than hidden:
    the window must be a browser, because Ctrl+A on a document editor selects
    the user's work.
    """
    from .channels import keyboard as kb
    hwnd = kb.foreground()
    if not hwnd:
        return None, "拿不到当前前台窗口"
    title = kb.window_text(hwnd) or ""
    exe = (kb.process_name(kb.window_pid(hwnd)) or "").lower()
    if exe not in _BROWSER_EXES:
        return None, (f"当前前台窗口是 {exe or '未知程序'}（{title[:40]!r}），"
                      f"不是浏览器；桌面通道不会对非浏览器窗口按 Ctrl+A")
    from .harvest import harvest_desktop
    card = _scratch_card(url)
    card.channel = "desktop"
    card.target = {"title": title, "restore": True, "url": url}
    try:
        rnd = harvest_desktop(card, page=1)
    except ChannelError as e:
        return None, str(e)
    # Deliberately NOT `rnd.captured.source_url = url`. This rung drove whatever
    # window was in front; it never navigated to `url` and cannot read that
    # window's address bar. Echoing the requested URL here was the bug: a
    # correctly-parsed, fully-conserved table attributed to the wrong page. The
    # requested URL stays in `requested_url`, `observed_url` stays empty, and the
    # report says the address is unknown.
    rnd.captured.requested_url = url
    rnd.captured.observed_url = ""
    return rnd.captured, ""


# ------------------------------------------------------------------- the ladder

_PINNED = ("http", "browser", "desktop")


def capture(url: str, channel: str = "auto", port: int | None = None
            ) -> tuple[Captured, Decision]:
    """Get `url` by the cheapest rung that works. Raises when none does.

    A pinned `channel` is obeyed exactly, and its failure is reported as-is. A
    pinned channel that silently escalates is worse than no pin at all: the
    caller asked for one specific mechanism and would be told the wrong one ran.
    """
    url = (url or "").strip()
    if not url:
        raise InputError("需要一个 URL", hint="例如 clip2table run --url https://...")
    if not url.lower().startswith(("http://", "https://")):
        raise InputError(f"只接受 http(s) 地址，收到 {url!r}")

    if channel in _PINNED:
        fn = {"http": lambda: try_http(url, PINNED_TIMEOUT),
              "browser": lambda: try_browser(url, port),
              "desktop": lambda: try_desktop(url)}[channel]
        cap, reason = fn()
        if cap is None:
            raise ChannelError(f"指定的通道 {channel} 拿不到内容：{reason}",
                               hint="去掉 --channel 就会按 A→B→C 自动降级")
        return cap, Decision(channel, cap.engine, "用户在 --channel 里明确指定")

    errors: list[str] = []

    cap, reason = try_http(url)
    if cap is not None:
        return cap, Decision("http", "HTTP",
                             f"直连就拿到了 {len(cap.payload or '')} 字符，"
                             f"不需要动浏览器")
    errors.append(f"A 直连：{reason}")

    cap, reason = try_browser(url, port)
    if cap is not None:
        return cap, Decision("browser", "E1",
                             f"直连不行（{reason}），改用真浏览器真按键")
    errors.append(f"B 浏览器：{reason}")

    cap, reason = try_desktop(url)
    if cap is not None:
        return cap, Decision("desktop", "E2",
                             "直连和浏览器都不行，改驱动已经打开的那个窗口")
    errors.append(f"C 桌面：{reason}")

    raise ChannelError(
        "三条通道都没拿到内容\n  " + "\n  ".join(errors),
        hint="A 需要页面免登录且服务端渲染；B 需要浏览器带 "
             "--remote-debugging-port 启动；C 需要那个页面已经在某个窗口里打开。"
             "先跑 doctor 看看到哪一步断了")
