"""harvest: one round of collection, start to finish, with nobody at the wheel.

The loop is the same for both engines -- only "who presses the keys" differs:

    focus target -> prepare (scroll) -> Ctrl+A -> Ctrl+C ->
    wait for the clipboard to actually change -> read -> parse -> dedupe ->
    next page

The wait is the part that matters. Sleeping a fixed 2 seconds looks fine on
the machine you developed on and reads the PREVIOUS page's data on a slow one.
That failure is silent and total: every row after the first page is off by one
page, and nothing errors. So the sync point is
`GetClipboardSequenceNumber()` changing, with a hard timeout that raises.
"""
from __future__ import annotations

import hashlib
import json as _json
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .cards.schema import Card
from .channels import cdp as cdp_mod
from .channels import keyboard as kb
from .channels.clipboard import read_clipboard, sequence_number
from .errors import ChannelError, InputError
from .models import Captured, ParseResult


class _EndOfList(Exception):
    """The list ran out. Not an error -- a stop condition.

    Kept separate from ChannelError on purpose: "there is no page 10" means the
    harvest is complete, while "page 3 came back as a login wall" means the run
    failed. Both used to exit 2 with the same wording, so a run that had
    collected everything looked identical to one that got blocked.
    """


# Making a hidden tab visible is racy: from an identical starting state the same
# Page.bringToFront flips it in some rounds and not others (measured 6/6 vs 4/6 in
# two runs of the same setup). One fixed sleep and one judgement turned that race
# into a hard error blaming the tab. Polling a budget, with a bounded number of
# attempts, costs the fast path nothing and absorbs the race.
_VISIBLE_BUDGET = 2.0
_VISIBLE_POLL = 0.1
_VISIBLE_ATTEMPTS = 3


@dataclass
class Round:
    """One page's worth of work, plus what actually happened."""

    captured: Captured
    result: ParseResult | None = None
    page: int = 1
    engine: str = ""
    channel: str = ""
    why: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class MultiRound:
    """Every page collected, plus the honest reason the loop stopped."""

    rounds: list[Round] = field(default_factory=list)
    stopped_because: str = ""
    pages_done: int = 0
    new_per_page: list[int] = field(default_factory=list)
    repeat_of: int | None = None     # page number this run repeated, if any

    @property
    def first(self) -> Round | None:
        return self.rounds[0] if self.rounds else None


# ------------------------------------------------------------------ waiting

def wait_for_clipboard(before: int, timeout_ms: int = 3000,
                       poll_ms: int = 40) -> None:
    """Block until the sequence number moves past `before`, or raise.

    "Moved" rather than "increased": the counter is a DWORD that a clipboard
    manager or an app restart can reset, and treating that as "no change"
    would time out on a copy that actually worked.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if sequence_number() != before:
            # The copy landed. Chromium fills the delayed formats (HTML) a
            # few milliseconds later, so a short settle beats a retry loop.
            time.sleep(0.06)
            return
        time.sleep(poll_ms / 1000.0)
    raise ChannelError(
        f"发了复制键，但 {timeout_ms} ms 内剪贴板没有变化",
        hint="页面可能没获得焦点、选区为空，或复制被站点拦截。"
             "先在目标里手动 Ctrl+A、Ctrl+C 试一次，能复制再跑")


# ------------------------------------------------------------------ browser

def harvest_browser(card: Card, page: int = 1, port: int | None = None,
                    client: cdp_mod.CDP | None = None,
                    target_url: str = "",
                    activate_tab: bool = True) -> Round:
    """E1: press keys through CDP. Borrows the OS foreground only to hand it back.

    `activate_tab` is not cosmetic. A hidden tab still receives the keystrokes
    but Chromium refuses to write the clipboard for it, so a harvest against a
    background tab times out with "the clipboard didn't change" - a message that
    blames the page for something the tool got wrong.

    Making it visible is not free, though. Measured: while the browser window is
    merely behind the user's editor, `Page.bringToFront` leaves the OS foreground
    alone; but when the browser window is MINIMISED, bringToFront restores the
    window and takes the foreground with it. So "it never touches your focus" is
    not a promise this code can keep. What it can promise is that the intrusion
    is brief and given back: the displaced window is recorded before the keys go
    and restored afterwards, and the notes say so either way.
    """
    harvest = card.harvest or {}
    own = client is None
    if client is None:
        client, target = cdp_mod.connect(port, _url_hint(card))
        target_url = str(target.get("url", ""))
    try:
        notes: list[str] = []

        # Whoever is in front right now gets the foreground back afterwards.
        # Recorded unconditionally because bringToFront may take it (measured
        # when the browser window is minimised) and the caller must not have to
        # guess whether that happened.
        prev_fg = kb.foreground()
        borrowed = False
        if activate_tab:
            borrowed = _make_visible(client, notes)
        try:
            # A tab sitting on Chromium's own error page is still a healthy tab:
            # Ctrl+A/Ctrl+C works, the clipboard fills, and the harvest "succeeds"
            # with the text of "this site can't be reached" turned into rows. The
            # failure is invisible unless the page is checked before the keys go.
            _check_page_usable(client, notes)
            _check_challenge(client, notes)

            # Optional: restrict selection to one container so the site's nav and
            # footer do not get swept into the copy.
            select_in = harvest.get("select_in")
            selected = False
            if select_in:
                selected = bool(client.eval(
                    "(() => { const el = document.querySelector(%r);"
                    " if (!el) return false;"
                    " const r = document.createRange(); r.selectNodeContents(el);"
                    " const s = window.getSelection(); s.removeAllRanges();"
                    " s.addRange(r); return true; })()" % select_in))
                if not selected:
                    notes.append(f"select_in={select_in!r} 页面上没找到，改为全选整页")

            if harvest.get("scroll"):
                _scroll(client, harvest["scroll"], notes)

            before = sequence_number()
            if not selected and harvest.get("select_all", True):
                client.key_combo(cdp_mod.VK_A)
            client.key_combo(cdp_mod.VK_C)
            wait_for_clipboard(before, int(harvest.get("timeout_ms", 3000)))

            cap = _read_with_retry(need_html=True)
            cap.channel = "browser"
            cap.engine = "E1"
            # E1 talks CDP to one specific tab, so the address is provable:
            # ask the tab itself where it is. `target_url` is only the value
            # from the moment we connected; a page that redirected since then
            # would make it a stale claim, and a stale provenance claim is the
            # one kind of wrong that no downstream check can expose.
            try:
                observed = str(client.eval("location.href") or "")
            except Exception:
                observed = ""
            cap.requested_url = str((card.target or {}).get("url") or "")
            cap.observed_url = observed or target_url
            cap.source_url = cap.observed_url
            cap.meta["target_url"] = cap.observed_url
            return Round(captured=cap, page=page, engine="E1", channel="browser",
                         why=f"CDP 真按键 → {cap.observed_url[:60]}", notes=notes)
        finally:
            # `notes` is the same list object the Round holds, so appending here
            # still lands in the report. Runs on the error path too: a failed
            # harvest must not leave the user's window behind ours.
            if borrowed:
                _give_back_foreground(prev_fg, notes)
    finally:
        if own:
            client.close()


def _give_back_foreground(prev_fg: int, notes: list[str]) -> None:
    """Undo the foreground change bringToFront may have caused. Never raises."""
    if not prev_fg or kb.foreground() == prev_fg:
        return
    kb.restore_focus(prev_fg)
    time.sleep(0.15)
    if kb.foreground() == prev_fg:
        notes.append("为复制临时把窗口提到前台，已把焦点还给原来的窗口")
    else:
        notes.append("为复制临时把窗口提到前台，但没能把焦点还回去"
                     "（可能是那个窗口已经关了或被别的程序抢走了）")


def _make_visible(client: cdp_mod.CDP, notes: list[str]) -> bool:
    """Make the tab visible so Chromium will allow the copy. Returns True if a
    `Page.bringToFront` was sent, which means the OS foreground may have moved.

    What was actually measured, because the earlier version of this docstring
    claimed the opposite:

      * tab already visible            -> copy works, no call made, foreground
                                          never moves
      * window visible, tab hidden     -> `Target.activateTarget` leaves it
                                          hidden and the copy fails;
                                          `Page.bringToFront` fixes it, and the
                                          foreground does NOT move
      * window minimised, tab hidden   -> `Page.bringToFront` restores the window
                                          and DOES take the foreground
      * any tab hidden, nothing else   -> no CDP call flips visibility without
                                          raising the window

    `Browser.setWindowBounds` was tried as a way to flip visibility without taking
    the foreground, and it is the wrong tool: in the exact state where the flip is
    needed it flipped 0/5 and still moved the foreground (`_probe/m2_4_fallback_ab.py`,
    `bringToFront` 5/5 in the same rounds). Raising the window is what brings a tab
    back, so bringToFront stays the only attempt.

    So visibility is a tab-level property and it is the precondition for the
    copy; the OS foreground is a side effect that cannot always be avoided.
    Callers get True back and must hand the foreground over. Sending the keys
    while hidden is the one thing that must never happen: it produces "复制键发了
    但剪贴板没变化", which blames the page for a condition the tool can see.
    """
    if client.eval("document.visibilityState") != "hidden":
        return False

    # Retry with polling rather than judging once after a fixed sleep. Measured:
    # the same Page.bringToFront flips the tab in some rounds and not others from
    # an identical starting state, and a second attempt in the same state does
    # work. Polling costs nothing when it is already visible (first read wins) and
    # turns a flaky hard error into a slower success.
    for attempt in range(_VISIBLE_ATTEMPTS):
        try:
            client.call("Page.bringToFront", {})
        except cdp_mod.CDPError as e:
            raise ChannelError(
                "这个标签页切不到可见状态，因此一个键都没发",
                hint=f"浏览器可能被最小化了，把它还原后重试（{e}）") from None
        deadline = time.perf_counter() + _VISIBLE_BUDGET
        while time.perf_counter() < deadline:
            if client.eval("document.visibilityState") != "hidden":
                return True
            time.sleep(_VISIBLE_POLL)
        if attempt < _VISIBLE_ATTEMPTS - 1:
            time.sleep(0.25)

    raise ChannelError(
        "标签页仍是隐藏状态，Chromium 会拒绝为它写剪贴板，因此没发按键",
        hint="把浏览器窗口还原（不是最小化）后重试；也可以手动点一下那个标签页")


def _check_page_usable(client: cdp_mod.CDP, notes: list[str]) -> None:
    """Refuse to harvest Chromium's network-error page.

    Measured: on such a tab `location.href` is `chrome-error://chromewebdata/`
    and `#main-frame-error` exists; on a real page both are false. `readyState`
    is "complete" and the body has text, so nothing else exposes it - the copy
    succeeds and the error text is parsed as data.

    The URL scheme is the discriminator rather than the page's text, because
    Chromium puts the failed URL in the title and the text is localized: on a
    Chinese-locale Edge the page says 无法访问此页面, on an English one it says
    "This site can't be reached". Neither string is ours to depend on.
    """
    href = str(client.eval("location.href") or "")
    if href.startswith("chrome-error://"):
        raise ChannelError(
            f"目标标签页停在浏览器的网络错误页（{href}），里面没有数据",
            hint="这个页面本身没加载出来，Ctrl+A 复制到的只是报错文字。"
                 "先在浏览器里刷新/换网络确认页面能打开，或者换一个能打开的标签页")


_WALL_JS = r"""
(() => {
  const VENDORS = ['challenges.cloudflare.com', 'newassets.hcaptcha.com',
                   'hcaptcha.com', 'google.com/recaptcha', 'recaptcha.net',
                   'geetest.com', 'geevisit.com', 'arkoselabs.com',
                   'funcaptcha.com', 'captcha-delivery.com'];
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const big = el => {
    const r = el.getBoundingClientRect();
    return r.width >= vw * 0.6 && r.height >= vh * 0.4;
  };
  const walls = [];
  for (const sel of ['#challenge-form', '#challenge-stage',
                     '.cf-browser-verification']) {
    if (document.querySelector(sel)) walls.push('marker:' + sel);
  }
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.src || '';
    const vendor = VENDORS.find(v => src.includes(v));
    if (vendor && big(f)) walls.push('iframe:' + vendor);
  }
  for (const sel of ['.geetest_panel', '.geetest_widget', '.h-captcha',
                     '.g-recaptcha', '.g-recaptcha-bubble-arrow']) {
    for (const el of document.querySelectorAll(sel)) {
      if (big(el)) walls.push('panel:' + sel);
    }
  }
  return JSON.stringify({walls: walls, title: document.title});
})()
"""


def _check_challenge(client: cdp_mod.CDP, notes: list[str]) -> None:
    """Stop when the page is a human-verification wall, before any key is sent.

    Why stop instead of pressing on: a challenge wall has no data behind it.
    Ctrl+A/Ctrl+C succeeds, and what lands in the clipboard is "Verify you are
    human" - so a harvest that ignores the wall returns rows of challenge text
    with a clean report. That is the failure mode this whole project exists to
    avoid: plausible-looking data that is not the data.

    Why not retry: solving a challenge is a human action. Retrying is exactly
    what makes a site escalate to a harder wall. The tool stops, says which
    wall it saw, and the user solves it in the same browser window - the session
    is already logged in, so the retry after solving runs in the same identity.

    What counts as a wall is measured, not assumed (`_probe/challenge_site.py`
    and `m2_wall_predicate.py`: 4 walls refused, 5 controls passed). A captcha
    BADGE pinned to a corner, and a login form with an inline Turnstile widget,
    both stay: they are small, and the page behind them is the data.
    """
    try:
        raw = client.eval(_WALL_JS)
        data = _json.loads(raw) if isinstance(raw, str) else None
    except Exception:  # noqa: BLE001 - a probe failure must not stop a harvest
        return
    walls = (data or {}).get("walls") or []
    if not walls:
        return
    shown = ", ".join(walls[:3])
    raise ChannelError(
        f"这个页面是人机验证墙（{shown}），里面没有数据，所以一个键都没发",
        hint="请在那个浏览器窗口里手动完成验证（图形/滑块/点击），"
             "页面跳回正常内容后再跑一次。我不会自动重试——"
             "重试只会让站点把验证升级得更狠")


def _scroll(client, spec, notes: list[str]) -> None:
    """Scroll until the page stops growing, so lazy lists load fully."""
    until = str(spec.get("until", "stable"))
    max_rounds = int(spec.get("max", 20))
    pause = int(spec.get("pause_ms", 500)) / 1000.0
    last = -1
    rounds = 0
    for _ in range(max_rounds):
        h = client.eval("document.body ? document.body.scrollHeight : 0")
        if until == "stable" and h == last:
            break
        last = h
        client.eval("window.scrollTo(0, document.body.scrollHeight)")
        rounds += 1
        time.sleep(pause)
    client.eval("window.scrollTo(0, 0)")
    notes.append(f"已滚到底（{until}，用了 {rounds} 轮）")


def click_next_page(client: cdp_mod.CDP, selector: str) -> bool:
    """Click the "next page" control. False when it is gone or disabled."""
    res = client.eval(
        "(() => { const a = document.querySelector(%r);"
        " if (!a) return 'missing';"
        " if (a.disabled || a.getAttribute('aria-disabled') === 'true')"
        "   return 'disabled';"
        " a.click(); return 'clicked'; })()" % selector)
    return res == "clicked"


# ------------------------------------------------------------------ desktop

def harvest_desktop(card: Card, page: int = 1, allow_force: bool = False) -> Round:
    """E2: Win32 SendInput into one specific window, then hand focus back."""
    tgt = card.target or {}
    harvest = card.harvest or {}
    hwnd = kb.find_target(title=tgt.get("title"),
                          exe=tgt.get("exe"),
                          window_class=tgt.get("class"))
    # Gate before anything is touched: the handle may already be stale.
    title = kb.check_identity(hwnd, tgt.get("title"), allow_force=allow_force)

    restore_to = kb.foreground()
    notes: list[str] = []
    how = kb.activate(hwnd)
    if how == "none":
        raise ChannelError(
            f"窗口 {title[:40]!r} 切不到前台，因此一个键都没发",
            hint="关掉抢焦点的程序（输入法候选框、置顶小窗），再重试")

    try:
        kb.require_foreground(hwnd)
        # Re-check after activation: focus could have moved during the switch.
        title = kb.check_identity(hwnd, tgt.get("title"), allow_force=allow_force)

        # Being in front is not the same as having the keyboard focus. WinUI3
        # apps (Windows 11 Notepad, and any WinAppSDK program) leave hwndFocus on
        # an input site that swallows text keys after a programmatic activation,
        # so Ctrl+A selects nothing and Ctrl+C copies nothing -- while SendInput
        # reports success. This hands the focus to the visible text control when
        # there is exactly one.
        foc = kb.focus_text_control(hwnd)
        if foc:
            notes.append(f"键盘焦点已放到文本控件上（{foc}）")

        for key in harvest.get("pre_keys", []):
            send_named_key(key)

        # A browser decides where Ctrl+A goes by Chromium's own idea of which
        # view has focus, and a programmatic activation does not restore that
        # idea: the OS foreground is the browser while the page still reports
        # document.hasFocus() == False. ESC hands focus back to the web contents
        # (12/12 measured, and harmless on an already-focused page), and the
        # check below catches the case where it did not.
        is_browser = (kb.process_name(kb.window_pid(hwnd)) or "").lower() in _BROWSER_EXES
        if is_browser:
            kb.press(kb.VK_ESCAPE)
            time.sleep(0.25)

        timeout = int(harvest.get("timeout_ms", 3000))
        cap: Captured | None = None
        reason = ""
        for attempt in range(3):
            before = sequence_number()
            if harvest.get("select_all", True):
                kb.combo(kb.VK_A)
                time.sleep(0.12)
            kb.combo(kb.VK_C)
            wait_for_clipboard(before, timeout)
            cap = _read_with_retry(need_html=True)
            reason = _chrome_copy_reason(cap, title) if is_browser else ""
            if not reason:
                break
            if attempt == 2:
                break
            notes.append(f"第 {attempt + 1} 次复制拿到的是浏览器界面（{reason}），"
                         f"把焦点交回页面再试")
            kb.press(kb.VK_ESCAPE)
            time.sleep(0.35)
        assert cap is not None
        if reason:
            raise ChannelError(
                f"复制到的是浏览器自己的界面（{reason}），不是页面内容",
                hint="程序化激活浏览器时页面拿不回键盘焦点，Ctrl+A 选中了地址栏。"
                     "把那个窗口点一下（让它真正在前台），或者带 --remote-debugging-port "
                     "启动浏览器改用 browser 通道")

        cap.channel = "desktop"
        cap.engine = "E2"
        # Desktop drives a WINDOW, not a URL. Ctrl+A on that window copies
        # whatever it currently shows, and no Win32 call can read a browser's
        # address bar, so the address is unprovable here. Leaving
        # `observed_url` empty is the honest outcome; copying the requested URL
        # into it would make a wrong-page result look verified. What IS provable
        # is the window's identity, so that is what gets recorded.
        cap.requested_url = str(tgt.get("url") or "")
        cap.observed_url = ""
        # `source_url` keeps the window title, which is the closest thing to an
        # "address" a desktop target can prove -- and it is a title, not a URL,
        # so callers must not treat it as one.
        cap.source_url = title
        cap.meta["target_title"] = title
        cap.meta["target_hwnd"] = hwnd
        cap.meta["target_pid"] = kb.window_pid(hwnd)
        cap.meta["focus_method"] = how
        return Round(captured=cap, page=page, engine="E2", channel="desktop",
                     why=f"Win32 真按键 → {title[:50]}（焦点方式 {how}）",
                     notes=notes)
    finally:
        if tgt.get("restore", True):
            if kb.restore_focus(restore_to):
                notes.append("焦点已还给原来的窗口")
            else:
                notes.append("⚠ 焦点没能还回去，可能停在目标窗口上")


def _desktop_target_hwnd(card: Card, allow_force: bool = False) -> int:
    tgt = card.target or {}
    hwnd = kb.find_target(title=tgt.get("title"), exe=tgt.get("exe"),
                          window_class=tgt.get("class"))
    kb.check_identity(hwnd, tgt.get("title"), allow_force=allow_force)
    return hwnd


_KEY_TABLE = ("ctrl+home", "ctrl+a", "ctrl+c", "pagedown", "pageup",
              "home", "end", "escape", "tab")


def send_named_key(key: str) -> None:
    """Only the keys in _KEY_TABLE. There is no text-typing path by design."""
    k = key.strip().lower()
    if k == "ctrl+home":
        kb.combo(kb.VK_HOME)
    elif k == "ctrl+a":
        kb.combo(kb.VK_A)
    elif k == "ctrl+c":
        kb.combo(kb.VK_C)
    elif k == "pagedown":
        kb.press(kb.VK_PAGEDOWN)
    elif k == "pageup":
        kb.press(kb.VK_PAGEUP)
    elif k == "home":
        kb.press(kb.VK_HOME)
    elif k == "end":
        kb.press(kb.VK_END)
    elif k == "escape":
        kb.press(kb.VK_ESCAPE)
    elif k == "tab":
        kb.press(kb.VK_TAB)
    else:
        raise InputError(f"不认识的按键 {key!r}",
                         hint=f"可用的只有：{', '.join(_KEY_TABLE)}")


# ------------------------------------------------------------------ shared

#: Browsers put Ctrl+A in the address bar when the page does not own focus.
#: Kept in sync with ladder._BROWSER_EXES (which decides whether a foreground
#: window may be driven at all).
_BROWSER_EXES = ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe",
                 "opera.exe", "vivaldi.exe")

_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.I)
_ADDRESS_BAR_RE = re.compile(
    r"^\s*<a\b[^>]*\bhref=[\"']https?://[^\"']+[\"'][^>]*>.*?</a>\s*$",
    re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _flatten(payload: str) -> str:
    """Best-effort plain text from either a text or an HTML payload."""
    return _TAG_RE.sub("", payload or "").strip()


def _chrome_copy_reason(cap: Captured, window_title: str) -> str:
    """Why this capture is browser UI rather than page content ('' = fine).

    A browser only writes the clipboard for the focused page. When its page has
    lost keyboard focus, Ctrl+A/Ctrl+C still succeeds -- it copies the browser's
    own chrome: the address bar (an `<a href="...">` wrapper in HTML, the bare
    URL in text) or the selected tab's title. That arrives as a perfectly normal
    capture and becomes a one-row "report" of the wrong thing.

    Measured: with the page unfocused, the copy came back as
    `<a href="http://127.0.0.1:8793/shell">梯子测试·公告列表</a>`, and pressing
    ESC first made the same keys copy 436 characters of real page text.
    """
    raw = cap.payload or ""
    flat = _flatten(raw)
    if not flat:
        return ""
    if len(flat) > 300:
        return ""
    # The address bar copies as a single link wrapping the page title.
    if _ADDRESS_BAR_RE.search(raw):
        return "复制到的是地址栏里的网址"
    if _URL_ONLY_RE.match(flat):
        return "复制到的是地址栏里的网址"
    # A browser window title is "<page title> - <profile> - Microsoft Edge".
    title = (window_title or "").strip()
    lead = re.split(r"\s+[-–—]\s+", title)[0].strip() if title else ""
    if lead and flat == lead:
        return "复制到的是标签页标题"
    return ""


def _read_with_retry(need_html: bool = True, tries: int = 3) -> Captured:
    """Read the clipboard, retrying while it is still being filled.

    An empty read right after a copy usually means the owning program is
    rendering the delayed formats, not that the copy failed.
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return read_clipboard(need_html=need_html)
        except ChannelError as e:
            last = e
            time.sleep(0.12 * (i + 1))
    assert last is not None
    raise last


def _url_hint(card: Card) -> str | None:
    h = card.harvest or {}
    return h.get("url_contains") or (card.fetch or {}).get("url_contains")


# --------------------------------------------------------------------- http

def can_restore(card: Card) -> tuple[bool, str]:
    """Whether this card can put the interface back where page N was.

    This is a property of the CARD, not the channel. "browser is resumable" was
    true only for lists with an addressable page URL; for a dynamic list whose
    URL never changes, saying "resume from page 4" is a claim the code cannot
    keep -- it copies whatever the tab happens to show and files it as page 4.

    Returns (ok, why). `why` is written for the user, not for a log.
    """
    if card.channel == "http":
        if "{page}" in str((card.fetch or {}).get("url") or ""):
            return True, "直连卡有 {page} 占位符，第 N 页就是一次 GET"
        return False, "直连卡的 [fetch].url 里没有 {page} 占位符，只能取一页"
    if card.channel == "browser":
        if card.paging_url_template:
            return True, f"浏览器卡有 url_template，可以直接导航到第 N 页"
        if card.paging_style == "click":
            return False, ("浏览器卡的列表地址不随页码变化，翻页只能点「下一页」；"
                           "没有 url_template 就无法确认标签页现在停在第几页")
        return False, "浏览器卡没有 [paging].url_template，无法导航到指定页"
    return False, f"{card.channel} 通道读不到当前界面位置，无法自动恢复"


def restore_to(client: cdp_mod.CDP, card: Card, page: int,
               notes: list[str] | None = None) -> str:
    """Navigate the tab to page `page` and prove it landed there.

    Only the URL-template path is implemented, because it is the only one that
    can be VERIFIED: after `Page.navigate` the address bar is read back and
    compared with what was asked for. Clicking "next" N-1 times cannot prove
    anything -- if the tab was already on page 3, three clicks land on page 6
    and the rows get filed as page 4.
    """
    notes = notes if notes is not None else []
    tmpl = card.paging_url_template
    if not tmpl:
        raise InputError(
            f"卡 {card.name!r} 没有 [paging].url_template，无法恢复到第 {page} 页",
            hint="在卡里加 url_template = \"https://.../list?page={page}\"，"
                 "或者用 --append-from-here 从当前屏幕接着采")
    want = tmpl.replace("{page}", str(page))
    client.call("Page.navigate", {"url": want})
    deadline = time.monotonic() + 20.0
    seen = ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            seen = str(client.eval("location.href") or "")
        except Exception:            # navigation tears down the old context
            continue
        ready = False
        try:
            ready = str(client.eval("document.readyState") or "") == "complete"
        except Exception:
            continue
        if ready and seen:
            break
    if not seen:
        raise ChannelError(
            f"导航到第 {page} 页（{want[:80]}）之后读不到地址栏",
            hint="确认标签页还在、没有被关闭或跳走")
    # Same page means same path+query; the fragment is not part of the address
    # of a page in a list, so a hash difference must not fail the check.
    want_key = urlsplit(want)
    got_key = urlsplit(seen)
    if (want_key.scheme, want_key.netloc, want_key.path, want_key.query) != \
            (got_key.scheme, got_key.netloc, got_key.path, got_key.query):
        raise ChannelError(
            f"想恢复到第 {page} 页，导航到了 {seen[:80]}，不是 {want[:80]}",
            hint="站点可能重定向了；用 --append-from-here 明确从当前屏幕接着采")
    notes.append(f"已导航到第 {page} 页：{want[:80]}")
    return seen


def harvest_http(card: Card, page: int = 1, timeout: float = 8.0) -> Round:
    """Channel A: GET the page and parse the response. No keys, no windows.

    This is the first rung of the ladder because it is the only one that needs
    nothing running. It is also the one that most often gets a wall instead of
    data, so a response that is not usable raises with the reason the site gave
    (HTTP 403, a JS shell, a login gate) rather than returning an empty round:
    the ladder needs to know *why* to escalate, and the user needs to know that
    the browser is about to open and it is not a bug.

    `[fetch].url` may carry `{page}`; without it, paging is refused instead of
    silently re-fetching page 1 and producing N copies of the same rows.
    """
    from .channels import http as ch

    fetch = card.fetch or {}
    url = str(fetch.get("url") or "")
    if not url:
        raise InputError(
            f"卡 {card.name!r} 的 channel 是 http，但 [fetch] 里没有 url",
            hint='在卡里加 [fetch] 段和 url = "https://..."')
    if "{page}" in url:
        url = url.replace("{page}", str(page))
    elif page > 1:
        raise InputError(
            f"翻到第 {page} 页，但 [fetch].url 里没有 {{page}} 占位符",
            hint='把 url 写成 "https://.../list?page={page}" 这样才翻得动')

    result = ch.fetch(url, timeout=timeout)
    must_contain = str(fetch.get("contains") or "")
    must_match = str(fetch.get("matches") or "")
    if not ch.usable_for_card(result, must_contain, must_match):
        # A 404/410 on page 2+ is not a broken card, it is the end of the list:
        # resuming a 9-page harvest with --max-pages 9 walks to page 10 and
        # used to die with "直连没拿到可用内容：HTTP 404" -- after having
        # collected everything the site had. "Ran out of pages" is an honest
        # stop, and it must not look like a failure of the run.
        if page > 1 and result.status in (404, 410):
            raise _EndOfList(f"第 {page} 页不存在（HTTP {result.status}）")
        raise ChannelError(
            f"直连没拿到可用内容：{result.reason or '返回的是空页或不是目标页'}",
            hint="这条站点要靠登录态或 JS 渲染，换 browser 通道"
                 "（channel = \"browser\"），或直接用 desktop 通道")

    cap = Captured(kind="html" if "<" in result.body[:200] else "text",
                   payload=result.body, source_url=result.url,
                   # `url` here is already page-substituted. Comparing the raw
                   # `.../plain/{page}` template against a real address would
                   # always differ, so every multi-page HTTP harvest would print
                   # "the address you got is not the one you asked for" -- a
                   # permanent false alarm that trains the user to ignore the
                   # one line that is supposed to catch redirects.
                   requested_url=url,
                   observed_url=result.url,
                   channel="http", engine="HTTP", formats=["http"])
    return Round(captured=cap, page=page, engine="HTTP", channel="http",
                 why=f"直连 HTTP {result.status} → {result.url[:80]}")


# ------------------------------------------------------------------ the loop

def harvest_pages(card: Card, max_pages: int = 1, port: int | None = None,
                  allow_force: bool = False, start_page: int = 1,
                  on_page=None) -> MultiRound:
    """Collect up to max_pages, stopping on any of four honest signals.

    "Reached max_pages" alone is a bad stop signal: it means the tool kept
    clicking into a list that had already run out, wasting the site's
    bandwidth and the user's time. The four conditions:

      1. max_pages reached
      2. the "next page" control is gone or did not move (see `_advance`)
      3. this page's content repeats one already seen (`_fingerprint`) - the
         pager "moved" but the list did not. This is the default policy.
      4. two consecutive pages added zero new rows - only when the card asks
         for it with `[paging].stop_when = "empty"`

    Condition 3 is the one that catches a page that never advances. Without
    it, a stuck pager looks like a successful 50-page harvest with 50 copies
    of page 1 -- the exact "conserved but wrong" failure this project exists
    to avoid. Condition 4 is off by default because a re-sorted or pinned
    list can legitimately hand back a page with no new rows while more
    records remain.

    `start_page` exists for resuming: a harvest killed on page 3 must be able
    to walk forward from page 3 instead of replaying 1 and 2 (which would look
    like success while doing nothing). `on_page` is called after every page is
    parsed and handed the round, so the caller can land it on disk before the
    next page is attempted -- a kill then costs one page, not the whole run.
    """
    out = MultiRound()
    if max_pages < 1:
        raise InputError("--max-pages 至少是 1")

    client = None
    target_url = ""
    seen: set[str] = set()
    empty_streak = 0
    fingerprints: dict[str, int] = {}
    empty_limit = max(1, int(getattr(card, "stop_after_empty", 2) or 2))
    use_empty_stop = str(getattr(card, "stop_when", "fingerprint")) != "fingerprint"

    def ensure_client() -> cdp_mod.CDP:
        nonlocal client, target_url
        if client is None:
            client, target = cdp_mod.connect(port, _url_hint(card))
            target_url = str(target.get("url", ""))
        return client

    try:
        if card.channel == "browser" and (start_page > 1 or card.paging_url_template):
            # Resuming is only real if the tab is actually moved back. Without
            # this, `start_page=4` means "copy whatever is on screen and call it
            # page 4" -- conserved, deduped, and from the wrong part of the list.
            cl = ensure_client()
            notes: list[str] = []
            seen_url = restore_to(cl, card, start_page, notes)
            target_url = seen_url
            for line in notes:
                print(line, file=sys.stderr)

        for page in range(start_page, start_page + max_pages):
            if card.channel == "browser":
                cl = ensure_client()
                rnd = harvest_browser(card, page=page, client=cl,
                                      target_url=target_url)
                # The tab moved, so the URL recorded on page 1 is a lie for
                # pages 2..N. Re-read it after the harvest, before advancing.
                target_url = str(cl.eval("location.href") or target_url)
                rnd.captured.source_url = target_url
                rnd.why = f"CDP 真按键 → {target_url[:80]}"
            elif card.channel == "http":
                try:
                    rnd = harvest_http(card, page=page)
                except _EndOfList as e:
                    out.stopped_because = f"列表到底了：{e}"
                    break
            else:
                rnd = harvest_desktop(card, page=page, allow_force=allow_force)

            # A pager that "succeeds" without moving hands back the same page.
            # Its rows are all duplicates, so every row-count signal says
            # "0 new" and the run either stops with the wrong reason or keeps
            # writing page 1 under a stream of page numbers. Comparing content
            # is the only signal that names what actually happened, so it is
            # checked before anything is committed: the repeat is NOT published.
            print_hash = _fingerprint(rnd.captured)
            if print_hash in fingerprints:
                first = fingerprints[print_hash]
                out.stopped_because = (
                    f"第 {page} 页和第 {first} 页内容完全相同（翻页没有真的前进），"
                    f"已停手，这一页没有写入")
                out.repeat_of = first
                break
            fingerprints[print_hash] = page

            # Parse right here, not at the caller: the stop conditions below are
            # expressed in ROWS, and counting rows is impossible without a
            # parsed round. Leaving `rnd.result` None made every page look like
            # "0 new rows", so the loop stopped after page 2 claiming it had hit
            # the end of the list - a false stop that reads like success.
            from .core import parse_capture
            res, _ = parse_capture(rnd.captured, card)
            rnd.result = res

            out.rounds.append(rnd)
            out.pages_done = page
            if on_page is not None:
                on_page(rnd)

            added = _count_new(rnd, seen, card.dedupe_key or None)
            out.new_per_page.append(added)
            empty_streak = empty_streak + 1 if added == 0 else 0

            if len(out.rounds) >= max_pages:
                out.stopped_because = f"达到 --max-pages {max_pages}"
                break

            # Explicit signals first, then the heuristic. A list with pinned or
            # re-sorted items can hand back a page with 0 new rows while more
            # records genuinely remain, so "no new rows" must never outrank
            # "the next-page control is gone" or a content repeat.
            if card.channel == "browser" and card.paging_url_template:
                target_url = restore_to(ensure_client(), card, page + 1)
                advanced = True
            else:
                advanced = _advance(card, client, allow_force)
            if not advanced:
                out.stopped_because = _no_advance_reason(card)
                break
            if use_empty_stop and empty_streak >= empty_limit:
                out.stopped_because = (
                    f"连续 {empty_streak} 页没有新增行，判定已到底"
                    f"（[paging].stop_when=\"{card.stop_when}\"，"
                    f"下一页按钮仍在，如果你确信后面还有数据，"
                    f"把它改成 \"fingerprint\" 就只按明确信号停）")
                break
            time.sleep(max(int(card.pause_ms), 0) / 1000.0)
    finally:
        if client is not None:
            client.close()

    return out


def _fingerprint(cap: Captured) -> str:
    """Content hash of one page, ignoring fields that legitimately vary.

    The point is to tell "the pager moved" from "the pager pretended to move".
    Only the payload goes in: URLs differ by page number on a stuck site, and
    timestamps differ on a page that reloaded itself.
    """
    body = cap.payload if cap.payload is not None else ""
    if not body and cap.json_payload is not None:
        body = repr(cap.json_payload)
    norm = "\n".join(" ".join(ln.split()) for ln in str(body).splitlines() if ln.strip())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def _advance(card: Card, client: cdp_mod.CDP | None,
             allow_force: bool) -> bool:
    """Move to the next page. False means the list is over."""
    if card.channel == "http":
        # Nothing to press: the next page is just another GET. Whether it has
        # content is decided by the row count after it is parsed, not here.
        return "{page}" in str((card.fetch or {}).get("url") or "")

    if card.channel == "browser":
        if client is None:
            return False
        if card.paging_style == "click" and card.paging_next:
            return click_next_page(client, card.paging_next)
        if card.paging_style == "key":
            client.key_simple(cdp_mod.VK_PAGEDOWN)
            return True
        return False

    # desktop: page key inside the same window
    if card.paging_style in ("key", "none"):
        hwnd = _desktop_target_hwnd(card, allow_force)
        if kb.activate(hwnd) == "none":
            return False
        kb.require_foreground(hwnd)
        send_named_key(str((card.target or {}).get("next_key", "pagedown")))
        return True
    return False


def _no_advance_reason(card: Card) -> str:
    if card.channel == "http":
        return "直连卡没有 {page} 占位符，只能取一页"
    if card.channel == "browser":
        if card.paging_style == "click":
            return f"「下一页」({card.paging_next}) 已消失或不可点"
        if card.paging_style == "key":
            return "按了 PageDown 仍没有新增（页面没滚出新内容）"
        return "卡里没有 [paging]，只取一页"
    if card.paging_style in ("key", "none"):
        return "按了翻页键仍没有新增（列表已经到底）"
    return f"desktop 卡不支持 paging.style={card.paging_style!r}"


def _count_new(rnd: Round, seen: set[str], keys: list[str] | None = None) -> int:
    """Rows on this page that were not on an earlier page. Fills `seen`.

    "New" has to mean the same thing here as it does in the dataset, or the
    report contradicts itself: counting whole-row values while the dataset
    dedupes on the card's key produced `每页新增：[3, 3, 3]` next to
    `新增 3 行` on a page list whose titles were stable and whose bodies
    drifted. Two numbers, same word, one of them wrong.
    """
    if rnd.result is None:
        return 0
    n = 0
    for row in rnd.result.rows:
        if keys:
            parts = [row.values.get(k) for k in keys]
            if not any(p not in (None, "") for p in parts):
                # No key: the dataset keeps every one of these, so does this.
                n += 1
                continue
            key = "\x1f".join("" if p is None else str(p).strip() for p in parts)
        else:
            key = "\x1f".join("" if v is None else str(v) for v in row.values.values())
        if key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def harvest(card: Card, page: int = 1, port: int | None = None,
            allow_force: bool = False) -> Round:
    """One page only. Kept as the seam `run`/`grab` use."""
    if card.channel == "browser":
        return harvest_browser(card, page=page, port=port)
    if card.channel == "desktop":
        return harvest_desktop(card, page=page, allow_force=allow_force)
    if card.channel == "http":
        return harvest_http(card, page=page)
    raise InputError(f"卡 {card.name!r} 没写 channel，harvest 需要它",
                     hint='在卡里加 channel = "browser" 或 channel = "desktop"')
