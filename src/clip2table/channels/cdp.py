"""Chrome DevTools Protocol: find the browser, pick the right tab, press keys.

Two things in here are not obvious and were both found the hard way:

1. `/json/list` returns *every* target, including internal ones. The first
   entry is often `edge://sync-confirmation-dialog/`. Sending Ctrl+A there
   types into a dialog nobody is looking at, and the copy silently produces
   the previous clipboard contents. So target selection filters by URL scheme
   and prefers a real page.

2. `Input.dispatchKeyEvent` reaches the renderer of a BACKGROUND tab, but the
   COPY does not happen there. Chromium writes the clipboard only when the tab
   is the focused WebContents, so a hidden tab receives Ctrl+A/Ctrl+C, fires
   the keydown handlers, and leaves the clipboard untouched. Measured, not
   guessed: on a hidden tab `keydown` fires and the sequence number stays put;
   the same keys on a visible tab change it.

   `Page.bringToFront` fixes that WITHOUT touching the OS foreground window.
   Verified with the browser minimized, with another app in front, and with
   none of the window's tabs selected: after bringToFront the tab reports
   `visibilityState == "visible"`, the copy lands, and `GetForegroundWindow`
   is still the other application. So E1 keeps its non-intrusive promise - it
   moves a tab inside the browser, never the user's window stack.

The modifier bit for Ctrl is 2. Both virtual key codes must be set (the
`windows*` one and the `native*` one) or Chromium ignores the event.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from ..errors import ChannelError
from .ws import WS, WSError

DEFAULT_PORTS = (9222, 9223, 9224, 9333)

# VK codes and the scan codes Chromium wants for them.
VK_A, VK_C, VK_HOME, VK_END, VK_PAGEDOWN = 0x41, 0x43, 0x24, 0x23, 0x22
SCAN = {VK_A: "KeyA", VK_C: "KeyC", VK_HOME: "Home", VK_END: "End",
        VK_PAGEDOWN: "PageDown"}
MOD_CTRL = 2


class CDPError(RuntimeError):
    """A CDP call returned an error or timed out."""


# --------------------------------------------------------------------- http

def http_json(port: int, path: str = "/json/list", timeout: float = 2.0):
    """GET a DevTools HTTP endpoint. Returns parsed JSON or None."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def find_debug_port(ports=DEFAULT_PORTS) -> int | None:
    """The first port that answers /json/version with a browser websocket."""
    for port in ports:
        info = http_json(port, "/json/version", timeout=1.0)
        if isinstance(info, dict) and info.get("webSocketDebuggerUrl"):
            return port
    return None


def browser_version(port: int) -> str:
    info = http_json(port, "/json/version", timeout=2.0) or {}
    return str(info.get("Browser", ""))


def list_targets(port: int) -> list[dict]:
    data = http_json(port, "/json/list", timeout=3.0)
    return data if isinstance(data, list) else []


def usable_pages(port: int) -> list[dict]:
    """Page targets a human could actually be looking at, best first."""
    out = []
    for t in list_targets(port):
        if t.get("type") != "page":
            continue
        url = str(t.get("url") or "")
        if url.startswith(("devtools://", "edge://", "chrome://",
                           "about:blank", "chrome-extension://")):
            continue
        if not t.get("webSocketDebuggerUrl"):
            continue
        out.append(t)
    out.sort(key=lambda t: 0 if str(t.get("url", "")).startswith("http") else 1)
    return out


def pick_target(port: int, url_contains: str | None = None) -> dict:
    """Choose the tab to drive.

    With several tabs open and no hint, this REFUSES instead of picking one.
    Picking "the first http page" silently drives the wrong tab: the copy
    succeeds, the clipboard fills with the wrong page, and the report looks
    clean. A human opening ten tabs is normal; guessing which one they meant is
    not something a tool should do on their behalf.
    """
    pages = usable_pages(port)
    if not pages:
        raise ChannelError(
            "浏览器开着了，但没有可用的页面标签",
            hint="在浏览器里打开要采集的页面，然后重试；"
                 "或者关掉浏览器用 doctor 给出的命令重新启动")
    if url_contains:
        hits = [t for t in pages if url_contains in str(t.get("url", ""))]
        if not hits:
            seen = "\n".join("    " + str(t.get("url", ""))[:90] for t in pages[:8])
            raise ChannelError(
                f"没有 URL 含 {url_contains!r} 的标签页",
                hint=f"现在开着的是：\n{seen}")
        return hits[0]
    if len(pages) > 1:
        seen = "\n".join(
            f"    [{i}] {t.get('title') or '(无标题)'}  —  {str(t.get('url'))[:80]}"
            for i, t in enumerate(pages[:10], 1))
        raise ChannelError(
            f"浏览器里有 {len(pages)} 个页面标签，没说清是哪个，我不猜",
            hint="在卡里写上要采哪一页，然后重跑：\n"
                 "    [harvest]\n"
                 '    url_contains = "想采的那个地址里的一小段"\n'
                 f"当前开着的页面：\n{seen}")
    return pages[0]


_BROWSER_CANDIDATES = (
    ("Edge", (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")),
    ("Chrome", (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LocalAppData%\Google\Chrome\Application\chrome.exe")),
)


def find_browser() -> tuple[str, str] | None:
    """(label, absolute exe path) for an installed browser, or None.

    Neither `msedge` nor `chrome` is on PATH on a stock Windows install, so a
    hint that says `msedge --remote-debugging-port=...` is not paste-ready: it
    answers "not recognized as an internal or external command". The path has
    to be the real one.
    """
    for label, paths in _BROWSER_CANDIDATES:
        for raw in paths:
            p = Path(os.path.expandvars(raw))
            if p.is_file():
                return label, str(p)
    return None


def hint_for_port(port: int = 9222) -> str:
    """A paste-ready PowerShell line, naming the browser actually installed.

    Two things are easy to get wrong here, and both were measured:

    - `msedge` / `chrome` are NOT on PATH on a stock Windows install, so a hint
      written as `msedge --flag` answers "not recognized as an internal or
      external command". The real path has to be in the line.
    - `& "<exe>" --flag` blocks the shell until the browser exits, which reads
      as a hang. `Start-Process` returns immediately.
    """
    found = find_browser()
    # The profile directory carries the port. Measured failure: a profile that
    # a leftover Edge instance already owns makes the new launch hand its flags
    # to that instance, which ignores --remote-debugging-port. The directory is
    # created, the port never opens, and nothing says why.
    # Double quotes: PowerShell only expands $env:TEMP inside them.
    profile_arg = f'"--user-data-dir=$env:TEMP\\c2t-profile-{port}"'
    if found:
        label, exe = found
        cmd = (f'Start-Process "{exe}" -ArgumentList '
               f"'--remote-debugging-port={port}', {profile_arg}")
        return (
            "浏览器不是用调试端口启动的。把下面这一行粘贴到 PowerShell 里执行，"
            f"再用同一个窗口打开要采集的页面（用你机器上的 {label}）：\n"
            f"  {cmd}"
        )
    return (
        "浏览器不是用调试端口启动的，也没在常见安装位置找到 Edge / Chrome。"
        "找到浏览器的 exe 后按这个形状执行：\n"
        f'  Start-Process "<你的浏览器路径>" -ArgumentList '
        f"'--remote-debugging-port={port}', {profile_arg}"
    )


# --------------------------------------------------------------------- client

class CDP:
    """Sequential id-call/reply over one websocket to one target."""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        self.ws = WS(ws_url, timeout=timeout)
        self._id = 0
        self.timeout = timeout

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        msg: dict = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        # Events are interleaved with replies: skip anything whose id differs.
        while True:
            raw = self.ws.recv()
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("id") != mid:
                continue
            if "error" in obj:
                err = obj["error"]
                raise CDPError(f"{method}: {err.get('message')}")
            return obj.get("result", {})

    def eval(self, expr: str, await_promise: bool = False):
        r = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if r.get("exceptionDetails"):
            det = r["exceptionDetails"]
            raise CDPError(f"页面里这段 JS 报错了：{det.get('text')}")
        return r.get("result", {}).get("value")

    # ------------------------------------------------------------- input
    def key_combo(self, vk: int, ctrl: bool = True) -> None:
        """A real Ctrl+<key>: keyDown with modifiers, then keyUp."""
        code = SCAN.get(vk, "")
        mods = MOD_CTRL if ctrl else 0
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind,
                "modifiers": mods,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "code": code,
                "key": _key_name(vk, ctrl),
                "text": "",
            })

    def key_simple(self, vk: int, code: str | None = None) -> None:
        """A bare key with no modifiers (PageDown, Home, Esc...)."""
        code = code or SCAN.get(vk, "")
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "code": code,
                "key": _key_name(vk, False),
                "text": "\r" if vk == 0x0D else "",
            })

    def insert_text(self, text: str) -> None:
        """Type text without touching the real keyboard. Probe/debug only."""
        self.call("Input.insertText", {"text": text})

    def close(self) -> None:
        self.ws.close()


def _key_name(vk: int, ctrl: bool) -> str:
    if ctrl:
        return {
            VK_A: "a", VK_C: "c", 0x56: "v", 0x58: "x", 0x46: "f",
        }.get(vk, "")
    return {
        VK_HOME: "Home", VK_END: "End", VK_PAGEDOWN: "PageDown",
        0x21: "PageUp", 0x0D: "Enter", 0x1B: "Escape", 0x20: " ",
        0x09: "Tab", 0x28: "ArrowDown", 0x26: "ArrowUp",
    }.get(vk, "")


def connect(port: int | None = None, url_contains: str | None = None,
            timeout: float = 20.0) -> tuple[CDP, dict]:
    """Find the port, pick a page, connect. Returns (client, target)."""
    port = port or find_debug_port()
    if port is None:
        raise ChannelError("没有找到浏览器的调试端口",
                           hint=hint_for_port())
    target = pick_target(port, url_contains)
    try:
        return CDP(str(target["webSocketDebuggerUrl"]), timeout=timeout), target
    except WSError as e:
        raise ChannelError(f"连不上这个标签页的 DevTools：{e}",
                           hint="关掉多余标签页后重试") from None
