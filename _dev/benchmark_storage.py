"""Measure collection, fixed-size append, query, replay and streaming export."""
from __future__ import annotations

import argparse
import ctypes
import json
import hashlib
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def process_io():
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]
    value = Counters()
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters)]
    if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(value)):
        raise RuntimeError("进程 IO 计数读取失败")
    return value.read_bytes, value.write_bytes


def peak_rss():
    class Memory(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "peak_paged", "paged", "peak_nonpaged",
                                                "nonpaged", "pagefile", "peak_pagefile")]
    value = Memory()
    value.cb = ctypes.sizeof(value)
    kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise RuntimeError("进程内存计数读取失败")
    return value.peak


def run(args):
    sys.path.insert(0, str(Path(args.source_root).resolve() / "src"))
    from clip2table import api, revisions as R, __version__
    from clip2table.cards.schema import Card
    from clip2table.pipeline.extract import FieldSpec
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    directory = Path(args.out).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    dataset = directory / "dataset"
    if dataset.exists() and not (args.query_only or args.existing):
        raise RuntimeError("每次测量使用新的输出目录")
    fields = ["id", "customer", "amount", "status", "version", "region"]
    offset = [0]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit
            page = int(parse_qs(urlsplit(self.path).query)["page"][0]) - 1
            first = offset[0] + page * args.rows
            rows = ["\t".join(fields)]
            for n in range(first, first + args.rows):
                rows.append(f"{n:012}\tcustomer-{n:012}\t10.00\topen\t1\tregion-01")
            raw = ("\n".join(rows) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    card = Card(name="resource-orders", channel="http", segment_mode="lines", skip_header=True,
        fields=[FieldSpec(name=name, kind="cell", col=n) for n, name in enumerate(fields)],
        dedupe_key=["id"], mutable_fields=["amount", "status", "version"], pause_ms=0,
        ledger={"sort": "business_version", "field": "version"},
        fetch={"url": f"http://127.0.0.1:{server.server_port}/orders?page={{page}}"})
    signature = hashlib.sha256()
    for file in sorted((Path(args.source_root) / "src").rglob("*.py")):
        signature.update(file.read_bytes())
    report = {"source_sha256": signature.hexdigest(), "version": __version__, "pages": args.pages, "rows_per_page": args.rows,
              "python": platform.python_version(), "platform": platform.platform(), "operations": [], "collection_trials": 1, "operation_trials": args.repeats}
    reads = {"parse_calls": 0, "capture_calls": 0, "body_bytes": 0}
    writes = {}
    original_parse, original_capture, original_dump = R.read_parse, R.read_capture, R._dump
    def read_parse(root, pid):
        reads["parse_calls"] += 1
        reads["body_bytes"] += (Path(root) / R.PARSES / pid / "rows.jsonl").stat().st_size
        return original_parse(root, pid)
    def read_capture(root, cid):
        reads["capture_calls"] += 1
        ref, cap = original_capture(root, cid)
        reads["body_bytes"] += ref.chars
        return ref, cap
    def dump(body, path):
        relative = Path(path).relative_to(dataset) if Path(path).is_relative_to(dataset) else Path("other")
        kind = relative.parts[0]
        writes[kind] = writes.get(kind, 0) + len((json.dumps(body, ensure_ascii=False, indent=1) + "\n").encode())
        return original_dump(body, path)
    R.read_parse, R.read_capture, R._dump = read_parse, read_capture, dump
    def measure(name, operation):
        before, io_before, write_before = dict(reads), process_io(), dict(writes)
        start = time.perf_counter()
        answer = operation()
        if hasattr(answer, "ok") and not answer.ok:
            raise RuntimeError(answer.message)
        io_after = process_io()
        item = {"name": name, "trial": 1 + sum(item["name"] == name for item in report["operations"]), "seconds": time.perf_counter()-start, "peak_rss": peak_rss(),
            **{key: reads[key]-before[key] for key in reads},
            "process_read_bytes": io_after[0]-io_before[0], "process_write_bytes": io_after[1]-io_before[1],
            "logical_metadata_bytes": {key: writes[key]-write_before.get(key, 0) for key in writes}}
        report["operations"].append(item)
        (directory / report_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(item), flush=True)
        return answer
    report_file = "restart-metrics.json" if args.query_only else "daily-metrics.json" if args.existing else "metrics.json"
    try:
        export_tag = str(time.time_ns()) if args.existing else "initial"
        def query():
            if hasattr(api, "query_view"):
                return api.query_view(dataset, limit=100)
            rows, fields, ledger = api.ledger_view(dataset)
            return rows[:100], fields, ledger
        if args.query_only:
            for _ in range(args.repeats):
                measure("query_after_restart", query)
            return
        if args.existing:
            offset[0] = len(R.list_captures(dataset)) * args.rows
            report["initial_pages"] = offset[0] // args.rows
            measure("prepare_existing", query)
        else:
            def collect():
                if not args.single_pages:
                    return api.execute(api.ExecuteRequest(dataset=str(dataset), max_pages=args.pages,
                                                          request_id="collect"), card)
                for page in range(args.pages):
                    offset[0] = page * args.rows
                    answer = api.execute(api.ExecuteRequest(dataset=str(dataset), max_pages=1,
                                                             request_id=f"collect-{page}"), card)
                    if not answer.ok:
                        return answer
                return answer
            first = measure("collect", collect)
            assert first.counts["dataset_rows"] == args.pages * args.rows
            offset[0] = args.pages * args.rows
        initial_offset = offset[0]
        for trial in range(args.repeats):
            offset[0] = initial_offset + trial * 10 * args.rows
            measure("append_10", lambda trial=trial: api.execute(api.ExecuteRequest(dataset=str(dataset), max_pages=10,
                                                                  request_id=f"append-{time.time_ns()}-{trial}"), card))
        for _ in range(args.repeats):
            measure("query_100", query)
        if args.full:
            for view in ("current", "history"):
                for fmt in ("csv", "xlsx"):
                    for trial in range(args.repeats):
                        extra = {"detail": "summary"} if "detail" in api.ExportRequest.__dataclass_fields__ else {}
                        measure(f"export_{view}_{fmt}", lambda view=view, fmt=fmt, trial=trial: api.export(api.ExportRequest(
                            dataset=str(dataset), view=view, format=fmt, out=str(directory / f"{view}-{export_tag}-{trial}.{fmt}"), **extra)))
            captures = [c.capture_id for c in R.list_captures(dataset)[:10]]
            altered = replace(card, fields=[replace(f, type="float") if f.name == "amount" else f for f in card.fields])
            for _ in range(args.repeats):
                measure("reparse_10", lambda: api.reparse(api.ReparseRequest(dataset=str(dataset), captures=captures), altered))
            if hasattr(api, "query_view"):
                from clip2table import index
                def rebuild():
                    index.rebuild(dataset)
                    return query()
                measure("rebuild_index", rebuild)
        report["directory_bytes"] = {folder.name: sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
                                     for folder in dataset.iterdir() if folder.is_dir()}
        (directory / report_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--out", required=True)
    parser.add_argument("--pages", type=int, required=True)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--query-only", action="store_true")
    parser.add_argument("--existing", action="store_true")
    parser.add_argument("--single-pages", action="store_true")
    run(parser.parse_args())
