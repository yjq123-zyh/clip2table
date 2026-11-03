"""Exercise owned browser/desktop sessions with local fixture data."""
from __future__ import annotations

import argparse
import json
import platform
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from clip2table import api, revisions
from clip2table.errors import ChannelError
from clip2table.cards.loader import resolve_card
from clip2table.channels import cdp, keyboard as kb


def browser():
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            number = 2 if "page=2" in self.path else 1
            content = (ROOT / "scenes/notice-list/input" / f"page-{number}.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    found = cdp.find_browser()
    if found is None:
        raise RuntimeError("需要 Edge 或 Chrome")
    with tempfile.TemporaryDirectory(prefix="c2t-browser-", ignore_cleanup_errors=True) as directory:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        url = f"http://127.0.0.1:{server.server_port}/notices?page=1"
        process = subprocess.Popen([found[1], "--headless=new", f"--remote-debugging-port={port}",
            "--no-first-run", "--disable-background-mode", f"--user-data-dir={directory}/profile", url], startupinfo=startup,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    client, _ = cdp.connect(port, "127.0.0.1")
                    client.close()
                    break
                except (ChannelError, cdp.CDPError, OSError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            card = resolve_card(str(ROOT / "scenes/notice-list/notices.toml"))
            card.harvest = {"url_contains": "127.0.0.1", "timeout_ms": 5000}
            card.paging_url_template = url.replace("page=1", "page={page}")
            card.max_pages = 2
            card.pause_ms = 0
            dataset = Path(directory) / "dataset"
            result = api.execute(api.ExecuteRequest(dataset=str(dataset), port=port), card)
            if not result.ok:
                raise RuntimeError(result.message)
            rows, _, _ = api.query_view(dataset)
            assert len(rows) == 3
            captures = revisions.list_captures(dataset)
            assert [c.observed_url for c in captures if c.page == 2] == [url.replace("page=1", "page=2")]
            assert all(c.meta.get("tab_id") for c in captures)
            return {"backend": found[0], "pages": result.counts["pages"], "entities": len(rows)}
        finally:
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False)
                process.wait(timeout=10)
            server.shutdown()
            server.server_close()


def desktop():
    import ctypes as C
    from ctypes import wintypes as W
    user, kernel = C.windll.user32, C.windll.kernel32
    user.CreateWindowExW.restype = W.HWND
    user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
        C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.GetClipboardData.restype = C.c_void_p
    user.SetClipboardData.argtypes = [W.UINT, C.c_void_p]
    user.SetClipboardData.restype = C.c_void_p
    kernel.GlobalSize.argtypes = [C.c_void_p]
    kernel.GlobalSize.restype = C.c_size_t
    kernel.GlobalLock.argtypes = [C.c_void_p]
    kernel.GlobalLock.restype = C.c_void_p
    kernel.GlobalUnlock.argtypes = [C.c_void_p]
    kernel.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
    kernel.GlobalAlloc.restype = C.c_void_p
    saved, window, edit = [], None, None
    copied = False
    events = []
    title = "clip2table-session-" + str(time.time_ns())
    ready = threading.Event()
    sample = "id\tvalue\r\nA1\t10\r\nA2\t20"
    previous = kb.foreground()
    def ui():
        nonlocal window, edit
        window = user.CreateWindowExW(0, "STATIC", title, 0x00CF0000, -30000, -30000, 400, 200, None, None, None, None)
        edit = user.CreateWindowExW(0, "EDIT", sample, 0x50000004, 0, 0, 380, 160, window, None, None, None)
        user.ShowWindow(window, 4)
        ready.set()
        message = W.MSG()
        while user.GetMessageW(C.byref(message), None, 0, 0) > 0:
            if 0x100 <= message.message <= 0x109:
                events.append((int(message.message), int(message.wParam)))
            user.TranslateMessage(C.byref(message))
            user.DispatchMessageW(C.byref(message))
    thread = threading.Thread(target=ui, daemon=True)
    thread.start()
    if not ready.wait(10) or not window or not edit:
        raise RuntimeError("窗口创建失败")
    try:
        if not user.OpenClipboard(window):
            raise RuntimeError("剪贴板暂不可用")
        try:
            fmt = 0
            while True:
                fmt = user.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                handle = user.GetClipboardData(fmt)
                size = kernel.GlobalSize(handle)
                if not size:
                    raise RuntimeError(f"无法完整保存剪贴板格式 {fmt}")
                pointer = kernel.GlobalLock(handle)
                try:
                    saved.append((fmt, C.string_at(pointer, size)))
                finally:
                    kernel.GlobalUnlock(handle)
        finally:
            user.CloseClipboard()
        copied = True
        kb.activate(window)
        kb.focus_text_control(window)
        user.SendMessageW(edit, 0x00B1, 0, -1)
        time.sleep(0.15)
        from clip2table.cards.schema import Card
        from clip2table.pipeline.extract import FieldSpec
        from clip2table.harvest import harvest
        from clip2table.core import parse_capture
        card = Card(name="desktop-check", channel="desktop", segment_mode="lines", skip_header=True,
            fields=[FieldSpec(name="id", kind="cell", col=0), FieldSpec(name="value", kind="cell", col=1)],
            target={"title": title, "restore": True}, harvest={"select_all": False, "pre_keys": ["escape"]})
        try:
            result, _ = parse_capture(harvest(card).captured, card)
        except ChannelError as error:
            raise RuntimeError({"error": error.message, "focus": kb._focus_owner(window),
                "edit": edit, "selection": user.SendMessageW(edit, 0x00B0, 0, 0),
                "length": user.GetWindowTextLengthW(edit), "events": events}) from error
        assert [row.values for row in result.rows] == [{"id": "A1", "value": "10"}, {"id": "A2", "value": "20"}]
        return {"backend": "Win32", "rows": len(result.rows), "clipboard_formats_restored": len(saved)}
    finally:
        if copied and user.OpenClipboard(window):
            try:
                user.EmptyClipboard()
                for fmt, data in saved:
                    handle = kernel.GlobalAlloc(2, len(data))
                    pointer = kernel.GlobalLock(handle)
                    C.memmove(pointer, data, len(data))
                    kernel.GlobalUnlock(handle)
                    if not user.SetClipboardData(fmt, handle):
                        raise RuntimeError("剪贴板恢复失败")
            finally:
                user.CloseClipboard()
        kb.restore_focus(previous)
        user.PostMessageW(window, 0x0010, 0, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=("browser", "desktop"))
    args = parser.parse_args()
    started = time.perf_counter()
    result = browser() if args.backend == "browser" else desktop()
    result.update(python=platform.python_version(), system=platform.platform(), seconds=time.perf_counter()-started)
    print(json.dumps(result, ensure_ascii=False))
