"""Durable collection, recovery, actions and exports across real reloads."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip2table import api, ledger, replay, revisions as R
from clip2table.cards.schema import parse_card
from clip2table.core import parse_capture
from clip2table.errors import ChannelError, InputError
from clip2table.harvest import MultiRound, Round
from clip2table.models import Captured

HEAD = "订单号\t客户\t状态\t更新时间\n"


def card():
    return parse_card({"schema": 1, "name": "orders", "channel": "http",
                       "segment": {"mode": "lines", "skip_header": True},
                       "fields": {n: {"kind": "cell", "col": i} for i, n in enumerate(
                           ("订单号", "客户", "状态", "更新时间"))},
                       "fetch": {"url": "http://example.invalid/orders?page={page}"},
                       "paging": {"style": "none", "max_pages": 2},
                       "dedupe": {"key": ["订单号"], "identity": ["客户"],
                                  "mutable": ["状态", "更新时间"]},
                       "ledger": {"sort": "business_time", "field": "更新时间", "timezone": "UTC"}})


class DurableOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "dataset"
        self.card = card()

    def text(self, name, rows):
        path = self.home / name
        path.write_text(HEAD + rows, encoding="utf-8")
        return path

    def execute(self, path, rid):
        return api.execute(api.ExecuteRequest(dataset=str(self.root), request_id=rid,
                                               text_file=str(path)), self.card)

    def test_two_runs_same_page_retain_history_and_scope(self):
        a = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n002\t乙\t已发货\t2026-01-01 10:00\n003\t丙\t待付款\t2026-01-01 10:00\n004\t丁\t已付款\t2026-01-01 10:00\n")
        b = self.text("b.txt", "001\t甲\t已付款\t2026-01-01 11:00\n002\t乙\t待付款\t2026-01-01 08:00\n003\t丙\t已付款\t2026-01-01 10:00\n005\t戊\t待付款\t2026-01-01 11:00\n")
        first, second = self.execute(a, "a"), self.execute(b, "b")
        self.assertTrue(first.ok, first.to_json())
        self.assertTrue(second.ok, second.to_json())
        rows, _, led = api.ledger_view(self.root, baseline=first.outputs["revision"])
        states = {r["订单号"]: r["状态"] for r in rows}
        self.assertEqual(states, {"001": "已付款", "002": "已发货", "003": "待付款", "004": "已付款", "005": "待付款"})
        self.assertEqual(len(led.history_rows()[0]), 8)
        self.assertEqual([c.kind for c in led.changes].count("new"), 1)
        self.assertEqual([c.kind for c in led.changes].count("updated"), 1)
        self.assertEqual(len(led.pending), 1)
        self.assertEqual([c.page for c in R.list_captures(self.root)], [1, 1])
        self.assertEqual(second.counts["unchanged"], 2)
        self.execute(b, "b")
        self.assertEqual(len(R.list_captures(self.root)), 2)
        self.execute(b, "c")
        rows, _, repeated = api.ledger_view(self.root)
        self.assertEqual(len(repeated.history_rows()[0]), 12)
        self.assertEqual(len(repeated.pending), 1)
        pending = repeated.pending[0]
        action = api.resolve(api.Request(dataset=str(self.root), request_id="resolve"), pending_id=pending.pending_id)
        self.assertEqual(action.status, api.NEEDS_ACTION, action.to_json())
        choice = next(c["candidate_id"] for c in pending.candidates if c["values"]["状态"] == "已付款")
        answer = api.ResumeRequest(dataset=str(self.root), request_id="answer",
                                   operation_id=action.operation_id, checkpoint=action.checkpoint,
                                   action_response={"action_id": action.action["action_id"], "candidate": choice})
        decided = api.resume(answer)
        self.assertTrue(decided.ok, decided.to_json())
        self.assertEqual(api.ledger_view(self.root)[2].pending, [])
        self.assertEqual({r["订单号"]: r["状态"] for r in api.ledger_view(self.root)[0]}["003"], "已付款")
        self.assertEqual(api.resume(answer).outputs, decided.outputs)

    def test_missing_receipt_reuses_committed_operation_and_detects_changed_input(self):
        path = self.text("source.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        first = self.execute(path, "one")
        api._receipt_path(self.root, "one").unlink()
        second = self.execute(path, "one")
        self.assertEqual(first.outputs, second.outputs)
        self.assertEqual(len(R.list_captures(self.root)), 1)
        path.write_text(HEAD + "002\t乙\t待付款\t2026-01-01 09:00\n", encoding="utf-8")
        conflict = self.execute(path, "one")
        self.assertEqual(conflict.error["code"], "request_conflict")
        self.assertEqual(len(R.list_captures(self.root)), 1)

    def test_saved_raw_recovery_uses_original_run_card_and_bytes(self):
        path = self.text("source.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        with patch("clip2table.core.parse_capture", side_effect=InputError("规则处理失败")):
            failed = self.execute(path, "first")
        self.assertEqual(failed.status, api.FAILED)
        self.assertEqual(len(R.list_captures(self.root)), 1)
        path.write_text("changed source", encoding="utf-8")
        changed_card = dataclasses.replace(self.card, fields=[])
        result = api.resume(api.ResumeRequest(dataset=str(self.root), request_id="continue",
                            run_id=failed.run_id, checkpoint=failed.checkpoint), changed_card)
        self.assertTrue(result.ok, result.to_json())
        self.assertEqual(result.run_id, failed.run_id)
        self.assertEqual(result.operation_id, failed.operation_id)
        self.assertEqual(len(R.list_runs(self.root)), 1)
        self.assertEqual(api.ledger_view(self.root)[0][0]["订单号"], "001")

    def test_action_round_trip_keeps_partial_progress_and_rejects_stale_answers(self):
        self.card.max_pages = 2
        captured = Captured(kind="text", payload=HEAD + "001\t甲\t待付款\t2026-01-01 09:00\n")
        def interrupted(_card, **kw):
            rnd = Round(captured=captured, page=1)
            kw["on_capture"](rnd)
            rnd.result = parse_capture(captured, _card)[0]
            kw["on_page"](rnd)
            raise ChannelError("需要验证", reason="verification_required")
        with patch("clip2table.harvest.harvest_pages", side_effect=interrupted):
            first = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id="start"), self.card)
        self.assertEqual(first.status, api.NEEDS_ACTION, first.to_json())
        self.assertEqual(json.loads(first.to_json())["status"], "needs_action")
        self.assertEqual(first.counts["committed"], 1)
        wrong = api.resume(api.ResumeRequest(dataset=str(self.root), request_id="wrong",
                           operation_id=first.operation_id, checkpoint=first.checkpoint,
                           action_response={"action_id": "foreign", "confirmed": True}))
        self.assertEqual(wrong.error["code"], "invalid_action")
        current = api.operation_status(api.ResumeRequest(dataset=str(self.root), operation_id=first.operation_id))
        self.assertEqual(current.checkpoint, first.checkpoint)
        def complete(_card, **kw):
            self.assertEqual(kw["start_page"], 2)
            cap = Captured(kind="text", payload=HEAD + "002\t乙\t已付款\t2026-01-01 10:00\n")
            rnd = Round(captured=cap, page=2)
            kw["on_capture"](rnd)
            rnd.result = parse_capture(cap, _card)[0]
            kw["on_page"](rnd)
            return MultiRound(rounds=[rnd], stopped_code="scope_limit_reached")
        with patch("clip2table.harvest.harvest_pages", side_effect=complete):
            final = api.resume(api.ResumeRequest(dataset=str(self.root), request_id="right",
                               operation_id=first.operation_id, checkpoint=first.checkpoint,
                               action_response={"action_id": first.action["action_id"], "confirmed": True}))
        self.assertTrue(final.ok, final.to_json())
        self.assertEqual(final.run_id, first.run_id)
        self.assertEqual(final.counts["committed"], 2)
        self.assertEqual(final.counts["this_request_pages"], 1)
        self.assertEqual(final.counts["this_request_rows"], 1)
        self.assertEqual(final.counts["captured_pages"], 2)

    def test_failed_reparse_survives_reload_and_recovers_only_failed_capture(self):
        a = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        b = self.text("b.txt", "002\t乙\t已付款\t2026-01-01 10:00\n")
        self.execute(a, "a")
        base = self.execute(b, "b").outputs["revision"]
        def fail_one(cap, chosen):
            if "002" in cap.payload:
                raise InputError("这份捕获未能处理")
            return parse_capture(cap, chosen)
        with patch("clip2table.replay.parse_capture", side_effect=fail_one):
            failed = api.reparse(api.ReparseRequest(dataset=str(self.root), request_id="parse", base=base), self.card)
        candidate = R.read_revision(self.root, failed.outputs["candidate"])
        self.assertFalse(candidate.operation_complete)
        self.assertEqual(len(candidate.failed), 1)
        with self.assertRaises(InputError):
            R.activate(self.root, candidate.revision_id, allow_incomplete=True)
        with patch("clip2table.replay.parse_capture", wraps=parse_capture) as parser:
            result = api.resume(api.ResumeRequest(dataset=str(self.root), request_id="retry-parse",
                                operation_id=failed.operation_id, checkpoint=failed.checkpoint))
        self.assertTrue(result.ok, result.to_json())
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(len(R.read_revision(self.root, result.outputs["candidate"]).captures), 2)
        self.assertEqual(R.active(self.root)["revision_id"], base)

    def test_rule_identity_and_selection_generation(self):
        R.init(self.root)
        original = R.write_rule(self.root, self.card)
        moved = dataclasses.replace(self.card, fetch={"url": "http://other.invalid"},
                                    ledger={"sort": "observed"})
        self.assertEqual(original.rule_id, R.write_rule(self.root, moved).rule_id)
        self.assertNotEqual(ledger.policy_from_card(self.card).policy_id,
                            ledger.policy_from_card(moved).policy_id)
        path = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        result = self.execute(path, "a")
        token = R.active_token(self.root)
        R.activate(self.root, result.outputs["revision"])
        stale = api.select_revision(api.SelectRequest(dataset=str(self.root), revision=result.outputs["revision"],
                                   expected_active_token=token))
        self.assertEqual(stale.error["code"], "revision_conflict")
        alternate = ledger.LedgerPolicy(keys=["订单号"], mutable=["状态", "更新时间"], sort="observed")
        chosen = api.select_revision(api.SelectRequest(dataset=str(self.root),
                                     revision=result.outputs["revision"], policy=alternate.as_dict()))
        self.assertTrue(chosen.ok, chosen.to_json())
        self.assertEqual(api.ledger_view(self.root)[2].policy.policy_id, alternate.policy_id)

    def test_export_receipt_recovery_and_real_render_companion(self):
        path = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        self.execute(path, "a")
        target = self.home / "out.xlsx"
        request = api.ExportRequest(dataset=str(self.root), request_id="xlsx", out=str(target), format="xlsx")
        result = api.export(request)
        self.assertTrue(result.ok, result.to_json())
        before = target.read_bytes()
        api._receipt_path(self.root, "xlsx").unlink()
        replayed = api.export(request)
        self.assertEqual(replayed.outputs, result.outputs)
        self.assertEqual(target.read_bytes(), before)
        long = self.text("long.txt", "002\t" + "甲" * 33000 + "\t已付款\t2026-01-01 10:00\n")
        self.execute(long, "long")
        rendered = api.export(api.ExportRequest(dataset=str(self.root), request_id="long-xlsx",
                              out=str(self.home / "long.xlsx"), format="xlsx"))
        self.assertTrue(rendered.ok, rendered.to_json())
        self.assertFalse(rendered.data["lossless"])
        self.assertIn("甲" * 33000, Path(rendered.outputs["companion"]).read_text(encoding="utf-8"))

    def test_process_lock_prevents_a_second_writer(self):
        R.init(self.root)
        script = "import sys; sys.path.insert(0, sys.argv[2]); from clip2table import revisions as r; from clip2table.errors import InputError\ntry:\n with r.writer(sys.argv[1]): pass\nexcept InputError as e:\n print(e.code)\n"
        with R.writer(self.root):
            proc = subprocess.run([sys.executable, "-S", "-c", script, str(self.root),
                                   str(Path(R.__file__).resolve().parents[1])], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "dataset_busy")

    def test_export_recovers_after_files_land_before_result_is_saved(self):
        path = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        self.execute(path, "a")
        request = api.ExportRequest(dataset=str(self.root), request_id="interrupted-export",
                                    format="xlsx", out=str(self.home / "out.xlsx"))
        save = api._save
        def fail_result(root, state):
            if state.get("operation") == "export" and state.get("results"):
                raise OSError("receipt storage unavailable")
            return save(root, state)
        with patch("clip2table.api._save", side_effect=fail_result):
            interrupted = api.export(request)
        self.assertEqual(interrupted.status, api.FAILED)
        target = self.home / "out.xlsx"
        before, modified = target.read_bytes(), target.stat().st_mtime_ns
        recovered = api.export(request)
        self.assertTrue(recovered.ok, recovered.to_json())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, modified)

    def test_legacy_page_without_raw_keeps_original_rows_and_unknown_metadata(self):
        R.init(self.root)
        source = self.home / "legacy"
        page = source / "pages" / "0000"
        page.mkdir(parents=True)
        (source / "state.json").write_text("{}", encoding="utf-8")
        meta = {"raw_available": False, "fields": ["订单号", "客户", "状态", "更新时间"],
                "keys": ["订单号"], "card_snapshot": {
                    "name": "legacy-orders", "segment": {"mode": "lines"},
                    "ledger": {"sort": "business_time", "field": "更新时间", "timezone": "UTC"},
                    "dedupe": {"keys": ["订单号"], "identity": ["客户"], "mutable": ["状态", "更新时间"]}}}
        (page / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (page / "rows.json").write_text(json.dumps([{"订单号": "001", "客户": "甲", "状态": "已付款", "更新时间": "2026-01-01 09:00"}]), encoding="utf-8")
        original = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        cref, pref = R.adopt_legacy_page(self.root, source, 0)
        self.assertFalse(cref.raw_available)
        self.assertEqual(pref.parser_build, "unknown")
        revision = R.write_revision(self.root, [(cref.capture_id, pref.parse_id)], policy=pref.legacy_policy)
        R.activate(self.root, revision.revision_id)
        self.assertEqual(api.ledger_view(self.root)[0][0]["状态"], "已付款")
        failed = api.reparse(api.ReparseRequest(dataset=str(self.root)))
        self.assertEqual(failed.status, api.FAILED)
        self.assertEqual(len(R.read_revision(self.root, failed.outputs["candidate"]).entries), 1)
        self.assertEqual(original, {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_typed_keys_preserve_precision_and_composite_boundaries(self):
        from clip2table.models import Row
        composite = ledger.LedgerPolicy(keys=["a", "b"])
        first = composite.key_of(Row(values={"a": "x\x1fy", "b": "z"}))[0]
        second = composite.key_of(Row(values={"a": "x", "b": "y\x1fz"}))[0]
        self.assertNotEqual(first, second)
        numeric = ledger.LedgerPolicy(keys=["id"], key_types={"id": "int"})
        self.assertEqual(numeric.key_of(Row(values={"id": "9007199254740993"}))[0], "int:9007199254740993")
        with self.assertRaises(ValueError):
            numeric.key_of(Row(values={"id": "1.5"}))

    def test_cancelled_returns_committed_progress(self):
        def stop(_card, **kw):
            cap = Captured(kind="text", payload=HEAD + "001\t甲\t待付款\t2026-01-01 09:00\n")
            rnd = Round(captured=cap, page=1)
            kw["on_capture"](rnd)
            rnd.result = parse_capture(cap, _card)[0]
            kw["on_page"](rnd)
            raise KeyboardInterrupt()
        with patch("clip2table.harvest.harvest_pages", side_effect=stop):
            result = api.execute(api.ExecuteRequest(dataset=str(self.root)), self.card)
        self.assertEqual(result.status, api.CANCELLED)
        self.assertEqual(result.counts["committed"], 1)
        self.assertTrue(result.checkpoint["run_id"])

    def test_cli_and_api_reparse_select_export_share_the_path(self):
        from clip2table.cli import main
        path = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        result = self.execute(path, "first")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            rc = main(["replay", "-d", str(self.root), "--json"])
        self.assertEqual(rc, 0, stream.getvalue())
        replayed = json.loads(stream.getvalue())
        candidate = replayed["outputs"]["candidate"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["rev", "select", candidate, "-d", str(self.root)]), 0)
            self.assertEqual(main(["ledger", "-d", str(self.root), "-f", "csv", "-o", str(self.home / "cli.csv")]), 0)
        exported = api.export(api.ExportRequest(dataset=str(self.root), out=str(self.home / "api.csv")))
        self.assertTrue(exported.ok)
        self.assertEqual((self.home / "cli.csv").read_bytes(), (self.home / "api.csv").read_bytes())

    def test_cli_resume_uses_frozen_card_after_original_file_is_removed(self):
        from clip2table.cli import main
        from clip2table import tasks
        sample = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        scene_card = Path(__file__).resolve().parents[1] / "scenes" / "order-ledger" / "order-ledger.toml"
        saved = self.home / "orders.toml"
        saved.write_bytes(scene_card.read_bytes())
        out = io.StringIO()
        with patch("clip2table.core.parse_capture", side_effect=InputError("暂时不能解析")), contextlib.redirect_stdout(out):
            code = main(["task", "run", "-p", str(saved), "-d", str(self.root),
                         "--text-file", str(sample), "--request-id", "cli-initial", "--json"])
        self.assertEqual(code, 2)
        failed = json.loads(out.getvalue())
        saved.unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["task", "run", "-d", str(self.root), "--resume", failed["run_id"],
                         "--request-id", "cli-continued", "--json"])
        self.assertEqual(code, 0, out.getvalue())
        resumed = json.loads(out.getvalue())
        self.assertEqual(resumed["run_id"], failed["run_id"])
        self.assertEqual(len(tasks.load(tasks.stats_path(saved))["runs"]), 1)

    def test_real_http_repeated_runs_start_at_page_one_and_keep_observations(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import os
        import threading
        state = {"rows": "001\t甲\t待付款\t2026-01-01 09:00\n002\t乙\t已发货\t2026-01-01 10:00\n"}
        requested = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                if self.path.endswith("page=1"):
                    body = (HEAD + state["rows"]).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.card.pause_ms = 0
            self.card.fetch["url"] = f"http://127.0.0.1:{server.server_port}/orders?page={{page}}"
            with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}):
                first = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id="http-a"), self.card)
                state["rows"] = "001\t甲\t已付款\t2026-01-01 11:00\n003\t丙\t待付款\t2026-01-01 11:00\n"
                second = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id="http-b"), self.card)
            self.assertTrue(first.ok, first.to_json())
            self.assertTrue(second.ok, second.to_json())
            self.assertEqual(requested, ["/orders?page=1", "/orders?page=2"] * 2)
            self.assertEqual(second.reason, "source_exhausted")
            self.assertEqual(len(api.ledger_view(self.root)[0]), 3)
            self.assertEqual(len(api.ledger_view(self.root, view="history")[0]), 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unknown_api_version_is_rejected_before_writing(self):
        result = api.execute(api.ExecuteRequest(dataset=str(self.root), api_version=99), self.card)
        self.assertEqual(result.error["code"], "unsupported_version")
        self.assertFalse(self.root.exists())

    def test_request_card_path_binds_actual_rules(self):
        scene_card = Path(__file__).resolve().parents[1] / "scenes" / "order-ledger" / "order-ledger.toml"
        saved = self.home / "orders.toml"
        saved.write_bytes(scene_card.read_bytes())
        sample = self.text("a.txt", "001\t甲\t待付款\t2026-01-01 09:00\n")
        request = api.ExecuteRequest(dataset=str(self.root), card=str(saved),
                                     text_file=str(sample), request_id="card-path")
        first = api.execute(request)
        self.assertTrue(first.ok, first.to_json())
        saved.write_text(saved.read_text(encoding="utf-8").replace("col  = 2", "col  = 3"), encoding="utf-8")
        self.assertEqual(api.execute(request).error["code"], "request_conflict")
        self.assertEqual(len(R.list_runs(self.root)), 1)

    def test_parse_diff_uses_full_source_coordinates_without_a_business_key(self):
        from clip2table.models import Row, ParseResult
        before = ParseResult(fields=["值", "旧字段"], rows=[Row(values={"值": "旧", "旧字段": "移除"}, raw_at=[3, 4])])
        after = ParseResult(fields=["值"], rows=[Row(values={"值": "新"}, raw_at=[3, 4])])
        diff = replay.diff_results(before, after)
        self.assertEqual({c.field_name for c in diff.changes}, {"值", "旧字段"})
        self.assertEqual(diff.ambiguous, [])


if __name__ == "__main__":
    unittest.main()
