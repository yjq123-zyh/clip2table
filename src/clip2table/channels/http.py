"""Channel A: ask the site directly, before driving a browser.

This is the cheapest channel and the only one that needs no running program, so
the ladder tries it first. It is also the one that fails most often, and that is
fine: a 403 or a script-only shell is a normal answer, not an error. What it must
never do is return a page that *looks* parsed but is really a login wall or a
"please enable JavaScript" placeholder, so the caller checks the body, not the
status code.

Deliberately no `requests`, no `httpx`, no `httpx`-adjacent dependency: urllib is
in the standard library and the whole point of the project is `pip install` with
zero required dependencies.

The one thing that is not obvious: a 200 response can carry an interstitial or a
soft login page, and those are indistinguishable from data at the HTTP layer.
`looks_like_shell()` exists to give the ladder a reason to escalate to the
browser instead of parsing a wall.
"""
from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
import zlib

from ..errors import ChannelError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Markers that a page is a shell or a gate rather than content. Kept short and
# literal on purpose: every extra entry is a way to misjudge a real page.
_SHELL_MARKERS = (
    "enable javascript",
    "please enable javascript",
    "请启用javascript",
    "请启用 javascript",
    "requires javascript",
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "challenge-form",
    "cf-challenge",
    "window.location.href=\"/login",
    "sign in to continue",
    "请先登录",
)


class FetchResult:
    """What channel A got. `ok` says whether it is worth parsing."""

    def __init__(self, url: str, status: int = 0, body: str = "",
                 content_type: str = "", reason: str = ""):
        self.url = url
        self.status = status
        self.body = body
        self.content_type = content_type
        self.reason = reason          # why it is not usable; empty when it is

    @property
    def ok(self) -> bool:
        return bool(self.body) and not self.reason

    def __repr__(self) -> str:
        return (f"FetchResult(status={self.status}, "
                f"chars={len(self.body)}, ok={self.ok}, "
                f"reason={self.reason!r})")


def _decode(raw: bytes, content_type: str, encoding: str) -> str:
    """Bytes to text, honouring the two compressions and the charset.

    Sites do lie about charset, but guessing wrong only costs one escalation to
    the browser, so this stays simple: declared charset, else UTF-8, else GBK --
    the last one because a wrong-charset Chinese government page is a common real
    case and mojibake is otherwise indistinguishable from a parse failure.
    """
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass

    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    declared = m.group(1) if m else ""
    for enc in (declared, "utf-8", "gbk"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def looks_like_shell(body: str) -> str:
    """Return why this body is not content, or "" when it looks like content.

    The distinction matters for the ladder: a shell must escalate to the browser,
    while a short body is merely a short body. So this looks for gates, not size.
    """
    low = body[:200_000].lower()
    for marker in _SHELL_MARKERS:
        if marker in low:
            return f"页面里有「{marker}」，是被拦截或需要 JS 的空壳"
    return ""


def fetch(url: str, timeout: float = 8.0, headers: dict[str, str] | None = None
          ) -> FetchResult:
    """GET `url` once. Never raises for an HTTP status: the caller decides."""
    if not url.lower().startswith(("http://", "https://")):
        raise ChannelError(f"channel A 只接受 http(s) 地址，收到 {url!r}")

    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "") or ""
            body = _decode(raw, ctype, (resp.headers.get("Content-Encoding")
                                        or "").lower())
            reason = looks_like_shell(body)
            return FetchResult(resp.geturl(), resp.status, body, ctype, reason)
    except urllib.error.HTTPError as e:
        # 401/403/404 are answers, not crashes: the ladder wants to know that the
        # site said no so it can go drive the real browser.
        return FetchResult(url, e.code, "",
                           e.headers.get("Content-Type", "") if e.headers else "",
                           f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return FetchResult(url, 0, "", "", f"连不上：{e}")


def usable_for_card(result: FetchResult, must_contain: str = "",
                    must_match: str = "") -> bool:
    """Is this response worth handing to the parser?

    `must_contain` is a literal every real page of this site has; `must_match` is
    a regex. Either one failing means the site answered with something else --
    a login form, an error page, a different locale -- and parsing it would
    silently produce rows from the wrong page.
    """
    if not result.ok:
        return False
    if must_contain and must_contain not in result.body:
        return False
    if must_match:
        try:
            if not re.search(must_match, result.body):
                return False
        except re.error:
            return False
    return True
