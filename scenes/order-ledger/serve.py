"""Serve the order samples as local HTML pages for the collection example."""
from __future__ import annotations

import argparse
import csv
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


class OrderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        page = parse_qs(url.query).get("page", ["1"])[0]
        if url.path != "/orders" or page not in ("1", "2", "3"):
            self.send_error(404)
            return
        with (ROOT / "input" / f"page-{page}.txt").open(encoding="utf-8") as source:
            rows = list(csv.reader(source, delimiter="\t"))
        table = []
        for i, row in enumerate(rows):
            tag = "th" if i == 0 else "td"
            table.append("<tr>" + "".join(f"<{tag}>{html.escape(v)}</{tag}>" for v in row) + "</tr>")
        next_page = str(int(page) + 1)
        next_link = (f'<a rel="next" href="/orders?page={next_page}">下一页</a>'
                     if next_page in ("2", "3") else "")
        body = ('<!doctype html><meta charset="utf-8"><title>订单台账</title>'
                + '<table id="orders">' + "".join(table) + "</table>" + next_link).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), OrderHandler) as server:
        print(f"订单示例：http://127.0.0.1:{server.server_port}/orders?page=1", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
