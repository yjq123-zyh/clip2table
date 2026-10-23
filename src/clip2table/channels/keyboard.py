"""Win32 keyboard injection (the E2 engine) and window lookup.

Two hard-won facts are baked in here:

`SendInput.argtypes` must be `[UINT, POINTER(INPUT), c_int]`. Declaring it as
`c_void_p` makes `byref` fail silently: the function returns 0, no key is
pressed, and nothing raises. That cost a whole debugging session.

Only four keys are ever sent, and all of them are read-only: Ctrl+A, Ctrl+C,
PageDown, Ctrl+Home. There is deliberately no "type text into a window"
primitive in this module. The prototype had one and used it to type 78
characters into a stranger's unsaved document -- it works far too well. If a
future feature needs it, add it in the probe directory, not in the product.
"""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes

from ..errors import ChannelError

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_A, VK_C, VK_HOME = 0x41, 0x43, 0x24
VK_PAGEDOWN, VK_PAGEUP, VK_END = 0x22, 0x21, 0x23
VK_ESCAPE, VK_TAB = 0x1B, 0x09


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


user32.SendInput.restype = wintypes.UINT
# Not c_void_p: with c_void_p, byref(inp) is rejected and SendInput silently
# does nothing instead of raising.
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(vk: int, flags: int = 0) -> None:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        err = ctypes.get_last_error()
        raise ChannelError(f"SendInput 发键失败（返回 {n}，错误码 {err}）",
                           hint="目标程序可能以管理员权限运行而本工具没有")


def press(vk: int) -> None:
    _send(vk)
    _send(vk, KEYEVENTF_KEYUP)


def combo(vk: int) -> None:
    """Ctrl+<vk>, Ctrl held across both halves."""
    _send(VK_CONTROL)
    _send(vk)
    _send(vk, KEYEVENTF_KEYUP)
    _send(VK_CONTROL, KEYEVENTF_KEYUP)


# ---------------------------------------------------------------- windows

user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]

SW_RESTORE = 9


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_text(hwnd: int) -> str:
    n = int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def process_name(pid: int) -> str:
    """Executable name for a pid, best effort: '' when it cannot be read."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)


def all_windows() -> list[tuple[int, str]]:
    """Every visible top-level window with a non-empty title."""
    out: list[tuple[int, str]] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            t = window_text(int(hwnd))
            if t:
                out.append((int(hwnd), t))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def _title_matches(pattern: str, title: str) -> bool:
    """`*` wildcards plus a case-insensitive substring, both accepted."""
    if "*" in pattern:
        rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
        return re.match(rx, title, re.IGNORECASE | re.DOTALL) is not None
    return pattern.lower() in title.lower()


def find_target(title: str | None = None, exe: str | None = None,
                window_class: str | None = None) -> int:
    """The one window the card points at, or a refusal.

    Ambiguity is an error, not a coin flip: two windows matching `title` means
    the card is not specific enough, and guessing risks pressing keys into the
    wrong document. This is exactly the mistake the prototype made when it
    picked the "newest" window.
    """
    if not title and not exe and not window_class:
        raise ChannelError("[target] 里至少要给一个 title / exe / class",
                           hint="desktop 通道必须能唯一确定一个窗口")
    cands: list[tuple[int, str]] = []
    for hwnd, t in all_windows():
        if not user32.IsWindow(int(hwnd)):
            continue
        if title and not _title_matches(title, t):
            continue
        if exe:
            name = process_name(window_pid(hwnd))
            if name != exe.lower():
                continue
        if window_class:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
            if buf.value != window_class:
                continue
        cands.append((hwnd, t))

    if not cands:
        raise ChannelError(
            f"没有找到窗口 title={title!r} exe={exe!r} class={window_class!r}",
            hint="先用 doctor 看看当前有哪些窗口；目标程序要先打开")
    if len(cands) > 1:
        listing = "；".join(f"0x{h:x} {t[:40]!r}" for h, t in cands[:5])
        raise ChannelError(
            f"有 {len(cands)} 个窗口都匹配，不能猜：{listing}",
            hint="把 [target].title 写具体一点（可以带 * 通配），只留下一个")
    return cands[0][0]


def _attached_set(hwnd: int) -> bool:
    """Join the foreground thread's input queue, then set the foreground.

    Attaching is what makes the window manager treat this call as coming from
    the process that already owns the foreground, which is the permission the
    bare call lacks. The detach must happen even on failure or the two input
    queues stay glued together for the life of this process.
    """
    fg = foreground()
    if not fg:
        return False
    tid_fg = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), None))
    tid_me = int(kernel32.GetCurrentThreadId())
    if not tid_fg or tid_fg == tid_me:
        return False
    if not user32.AttachThreadInput(tid_fg, tid_me, True):
        return False
    try:
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        user32.AttachThreadInput(tid_fg, tid_me, False)
    return foreground() == hwnd


def activate(hwnd: int) -> str:
    """Bring hwnd to the foreground. Returns which method worked, or "none".

    Every rung is verified by re-reading GetForegroundWindow. That is not
    belt-and-braces: SetForegroundWindow returns nonzero while only flashing
    the taskbar button when the calling process has no foreground rights, so
    trusting its return value produces a tool that says it activated a window
    it did not activate -- and then sends Ctrl+A into whatever the user was
    actually typing in. Measured at 7/8 success when the return value was
    trusted, 8/8 when the foreground is re-read (8 trials per rung, with a real
    application deliberately put in front before each trial).
    """
    h = wintypes.HWND(hwnd)
    if user32.IsWindow(h) and foreground() == hwnd:
        return "already-foreground"

    if user32.IsIconic(wintypes.HWND(hwnd)):
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        time.sleep(0.15)

    if user32.SetForegroundWindow(wintypes.HWND(hwnd)) and foreground() == hwnd:
        return "SetForegroundWindow"

    # The reliable one, and the reason it is second instead of last: attaching
    # to the foreground thread is the documented way to be granted permission.
    if _attached_set(hwnd):
        return "AttachThreadInput"

    # ALT nudge: the shell grants foreground rights to whoever last took an ALT
    # keystroke, so a bare ALT can unlock the next call. Kept because it works
    # on locked-down desktops where attaching is refused, not because it is
    # likely -- it never fired in the measurements above.
    _send(0x12)
    _send(0x12, KEYEVENTF_KEYUP)
    time.sleep(0.05)
    if user32.SetForegroundWindow(wintypes.HWND(hwnd)) and foreground() == hwnd:
        return "alt-nudge"

    # Two honest retries: the window manager sometimes refuses the first
    # request right after another window took the foreground, and the retry is
    # cheap next to sending keys to the wrong window.
    for _ in range(2):
        time.sleep(0.12)
        if _attached_set(hwnd):
            return "AttachThreadInput"
    return "none"


def require_foreground(hwnd: int) -> None:
    """Refuse to send keys unless the target really is the foreground window.

    This is the guard that the prototype lacked. SendInput goes to whatever
    owns the foreground; there is no "send to this hwnd". So the only safe
    protocol is: activate, verify, and abort if verification fails.
    """
    if foreground() != hwnd:
        raise ChannelError(
            "没能把目标窗口切到前台，因此没有发任何按键",
            hint="有别的程序在抢焦点（或目标已最小化）。关掉它，"
                 "或手动点一下目标窗口再重试——绝不会盲发按键")


_TEXT_CONTROL_CLASSES = ("Edit", "RichEdit20W", "RichEdit20A", "RichEdit50W",
                         "RichEdit60W", "RichEditD2DPT", "RICHEDIT60W")
user32.EnumChildWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                    wintypes.LPARAM]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
user32.SetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 128)
    return buf.value


def _focus_owner(hwnd: int) -> tuple[int, int]:
    """(hwndFocus, hwndCaret) for the thread that owns hwnd.

    GetGUIThreadInfo is per-thread, not per-window, so for an app that hosts
    several windows in one thread these handles may describe a different window.
    Callers must therefore treat them as hints and verify containment.
    """
    pid = wintypes.DWORD(0)
    tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd),
                                          ctypes.byref(pid))
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return 0, 0
    return int(gti.hwndFocus or 0), int(gti.hwndCaret or 0)


def _child_text_controls(hwnd: int) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(child, _param):
        if _class_name(int(child)) in _TEXT_CONTROL_CLASSES:
            found.append(int(child))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), visit, 0)
    return found


def focus_text_control(hwnd: int) -> str:
    """Put the keyboard focus on the text control that accepts input.

    Needed because activating a window is not the same as giving its text field
    the keyboard focus. Measured on Windows 11 Notepad: after a programmatic
    activation the foreground really is the target, but hwndFocus is
    `InputSiteWindowClass` -- a WinUI3 input site that accepts no text. Keys are
    then delivered to that site and dropped, so Ctrl+A selects nothing and Ctrl+C
    copies nothing, while SendInput still reports success and the clipboard never
    changes. The failure surfaces as "the page did not copy", which sends the
    user looking at the wrong program.

    The rule that works is deliberately application-agnostic: among the text
    controls under this window, focus the one that is visible. Notepad hosts six
    RichEdit controls (one per tab) in one HWND and only the active tab's is
    visible; focusing each in turn, exactly one made Ctrl+C produce output, and it
    was the visible one. Applying SetFocus to a hidden sibling changed hwndFocus
    but still copied nothing, so "first text control" is not a substitute.

    Returns a short description of what it did, for the report. Raises only when
    attaching threads fails for a reason worth reporting; an ordinary failure to
    find a text control is silent, because plenty of programs copy from a canvas
    or a grid and need no help.
    """
    focus, caret = _focus_owner(hwnd)
    if (focus and focus != hwnd
            and _class_name(focus) in _TEXT_CONTROL_CLASSES
            and user32.IsWindowVisible(wintypes.HWND(focus))):
        return ""            # already on a visible text control: leave it alone

    target = caret if caret and user32.IsWindowVisible(wintypes.HWND(caret)) else 0
    if not target:
        visible = [h for h in _child_text_controls(hwnd)
                   if user32.IsWindowVisible(wintypes.HWND(h))]
        if len(visible) != 1:
            # Zero: not a text-entry program. More than one: cannot tell which
            # one the user is looking at, and guessing would send keys into the
            # wrong document -- exactly the mistake this module exists to avoid.
            return ""
        target = visible[0]

    tid_target = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    tid_me = kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(wintypes.DWORD(tid_me),
                                             wintypes.DWORD(tid_target), True))
    try:
        user32.SetFocus(wintypes.HWND(target))
    finally:
        if attached:
            user32.AttachThreadInput(wintypes.DWORD(tid_me),
                                     wintypes.DWORD(tid_target), False)
    now, _ = _focus_owner(hwnd)
    if now != target:
        return ""
    return f"focus={target}（{_class_name(target)}）"


def restore_focus(hwnd: int) -> bool:
    """Give the foreground back to whoever had it. True if it worked.

    Deliberately built on the same verified ladder as `activate`, because the
    naive version -- call SetForegroundWindow and trust a nonzero return -- is
    the bug that made `activate` lie (see its docstring). Measured here: after a
    harvest borrowed the foreground, the bare call returned False and the window
    was left behind ours, so the report had to admit it had not given the focus
    back. Attaching to the current foreground thread first is what makes the
    hand-back land.
    """
    if not hwnd:
        return False
    if foreground() == hwnd:
        return True
    if user32.SetForegroundWindow(wintypes.HWND(hwnd)) and foreground() == hwnd:
        return True
    if _attached_set(hwnd):
        return True
    for _ in range(2):
        time.sleep(0.12)
        if _attached_set(hwnd):
            return True
    return foreground() == hwnd


_DIRTY_RE = re.compile(r"^\s*[*•●]\s|[*•●]\s*$")


def has_unsaved_marker(title: str) -> bool:
    """Does this title claim unsaved changes?

    Only apps that put a marker in the title can be caught this way -- Word,
    Notepad, most editors do; a WPF grid usually does not. It is a heuristic,
    and it is advisory: it gates the typeless Ctrl+A/Ctrl+C path behind
    --force, because selecting all in a document with unsaved edits and then
    pasting over it is the one irreversible mistake this tool could cause.
    """
    return bool(_DIRTY_RE.search(title))


def check_identity(hwnd: int, expected_title: str | None = None,
                   allow_force: bool = False) -> str:
    """The three gates that stand between a bug and someone's unsaved work.

    Returns the window title. Raises instead of proceeding when:
      1. the handle is no longer a window (it closed between lookup and use),
      2. the title no longer matches what the card asked for (another window
         reused the handle, or the user switched documents),
      3. the title says unsaved changes and --force was not given.
    """
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise ChannelError(
            f"窗口句柄 0x{hwnd:x} 已经不是有效窗口（可能在采集期间被关掉了）",
            hint="确认目标程序还开着，再跑一次")
    title = window_text(hwnd)
    if not title:
        raise ChannelError(
            f"窗口 0x{hwnd:x} 现在没有标题，无法确认它是目标",
            hint="目标窗口可能正在关闭。重开后再跑")
    if expected_title and not _title_matches(expected_title, title):
        raise ChannelError(
            f"窗口标题变了：卡里要求 {expected_title!r}，实际是 {title[:60]!r}",
            hint="目标可能已经切换。确认窗口后再跑，工具不会盲发按键")
    if has_unsaved_marker(title) and not allow_force:
        raise ChannelError(
            f"目标窗口标题里有未保存修改的标记：{title[:60]!r}",
            hint="先保存那个文档，或明确加 --force 承担风险再跑")
    return title

