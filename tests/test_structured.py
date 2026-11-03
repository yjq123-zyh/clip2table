"""JSON/DOM extraction through saved rules, observations and application operations."""
import contextlib
import io
import json
import tempfile
import tomllib
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from clip2table import api, harvest, replay, revisions
from clip2table.cards import emit
from clip2table.cards.schema import parse_card
from clip2table.cli import main, _rewrite_field
from clip2table.core import parse_capture
from clip2table.errors import ChannelError, InputError
from clip2table.models import Captured
from clip2table.pipeline.structured import Document, select


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "structured"
JSON = (FIXTURES / "orders-json" / "orders.json").read_text(encoding="utf-8")


def config(mode="json"):
    return tomllib.loads((FIXTURES / f"orders-{mode}" / "card.toml").read_text(encoding="utf-8"))


def card_file(path, card):
    lines = ["schema = 2", f'name = "{card.name}"', 'channel = "http"',
             '[fetch]', 'url = "https://example.test/orders?page={page}"']
    lines.extend(emit.segment_block(card))
    for spec in card.fields:
        lines.extend(emit.field_block(spec))
    path.write_text("\n".join(lines), encoding="utf-8")


HTML = (FIXTURES / "orders-dom" / "orders.html").read_text(encoding="utf-8")


class TestJSON(unittest.TestCase):
    def test_precision_null_missing_and_false_survive(self):
        data = config()
        for name, typ in (("flag", "bool"), ("zero", "int"), ("empty", "text"), ("null", "text"), ("missing", "text")):
            data["fields"][name] = {"kind": "json", "path": "/" + name, "type": typ}
        res, _ = parse_capture(Captured(kind="json", payload=JSON), parse_card(data))
        row = res.rows[0]
        self.assertEqual(row.values, {"id": 9007199254740993, "amount": "1.2300",
                         "flag": False, "zero": 0, "empty": "", "null": None, "missing": None})
        self.assertEqual(row.traces["null"].status, "null")
        self.assertEqual(row.traces["missing"].status, "missing")
        self.assertEqual(row.traces["amount"].locator["path"], "/data/orders/0/amount")

    def test_escaped_keys_and_array_indices(self):
        data = config()
        data["fields"]["amount"]["path"] = "/a~1b~0c/1"
        raw = '{"data":{"orders":[{"id":1,"a/b~c":["x","yes"]}]}}'
        res, _ = parse_capture(Captured(kind="json", payload=raw), parse_card(data))
        self.assertEqual(res.rows[0].get("amount"), "yes")

    def test_empty_array_and_invalid_record_coverage(self):
        task = parse_card(config())
        for members, candidates, parsed in (([], 0, 0), ([1, {"id": 2}], 2, 1)):
            raw = json.dumps({"data": {"orders": members}})
            res, _ = parse_capture(Captured(kind="json", payload=raw), task)
            self.assertEqual(res.coverage["candidates"], candidates)
            self.assertEqual(res.coverage["parsed"], parsed)
            self.assertEqual(len(res.coverage["pending"]), candidates - parsed)

    def test_missing_keys_keep_records_and_dedupe_has_mapping(self):
        raw = '{"data":{"orders":[{}, {}, {"id":1}, {"id":1}]}}'
        res, _ = parse_capture(Captured(kind="json", payload=raw), parse_card(config()))
        self.assertEqual(len(res.rows), 3)
        self.assertEqual(res.coverage["deduplicated"], 1)
        self.assertEqual([m["row"] for m in res.coverage["mapping"]], [0, 1, 2, 2])

    def test_non_array_returns_action(self):
        out = api.preview(api.PreviewRequest(), parse_card(config()),
                          Captured(kind="json", payload='{"data":{"orders":{}}}'))
        self.assertEqual((out.status, out.reason), ("needs_action", "structure_selection"))

    def test_invalid_paths_and_json_are_rejected(self):
        for path in ("data/orders", "/bad~x"):
            data = config()
            data["segment"]["path"] = path
            with self.assertRaises(InputError):
                parse_card(data)
        with self.assertRaises(InputError):
            parse_capture(Captured(kind="json", payload='{"a":NaN}'), parse_card(config()))


class TestDOM(unittest.TestCase):
    def test_relative_fields_attributes_and_node_locations(self):
        data = config("dom")
        data["fields"]["link"] = {"kind": "dom", "sel": "a", "attr": "href"}
        res, _ = parse_capture(Captured(kind="html", payload=HTML), parse_card(data))
        self.assertEqual(res.rows[0].values, {"id": "A1", "amount": "1.2300", "link": "/one"})
        self.assertEqual(res.rows[0].traces["link"].locator["attr"], "href")
        self.assertTrue(res.rows[0].locator["path"])

    def test_multiple_field_matches_are_explicit(self):
        html = HTML.replace('<span class="amount">1.2300</span>', '<span class="amount">1</span><span class="amount">2</span>')
        data = config("dom")
        res, _ = parse_capture(Captured(kind="html", payload=html), parse_card(data))
        self.assertIsNone(res.rows[0].get("amount"))
        self.assertEqual(res.rows[0].traces["amount"].status, "ambiguous")
        data["fields"]["amount"]["match"] = "first"
        res, _ = parse_capture(Captured(kind="html", payload=html), parse_card(data))
        self.assertEqual(res.rows[0].get("amount"), "1")

    def test_region_count_and_nested_records_return_action(self):
        for html in ("<p>empty</p>", HTML + HTML, HTML.replace("</article>", '<article class="order"></article></article>')):
            with self.subTest(html=html), self.assertRaises(ChannelError) as caught:
                parse_capture(Captured(kind="html", payload=html), parse_card(config("dom")))
            self.assertEqual(caught.exception.reason, "structure_selection")

    def test_selector_subset_and_document_order(self):
        root = Document('<main><a class="x" data-v="a b">1</a><div><a class="x">2</a></div></main>').root
        self.assertEqual([n.text() for n in select(root, 'main > a.x[data-v="a b"]')], ["1"])
        self.assertEqual([n.text() for n in select(root, "main .x")], ["1", "2"])
        with self.assertRaises(InputError):
            select(root, "a:hover")

    def test_browser_waits_for_region_and_saves_snapshot(self):
        data = config("dom")
        data["channel"] = "browser"
        task = parse_card(data)
        client = Mock()
        snapshots = iter(['<html></html>', HTML])
        def evaluate(expression):
            if expression == "document.documentElement.outerHTML":
                return next(snapshots)
            if expression == "location.href":
                return "https://example.test/orders"
            return '{}'
        client.eval.side_effect = evaluate
        with patch.object(harvest.time, "sleep"), patch.object(harvest.kb, "foreground") as foreground:
            rnd = harvest.harvest_browser(task, client=client)
        self.assertEqual(rnd.captured.payload, HTML)
        foreground.assert_not_called()
        client.key_combo.assert_not_called()


class TestPersistentOperations(unittest.TestCase):
    def test_shipped_card_samples_match_independent_answers(self):
        from clip2table.cards.regress import test_card
        from clip2table.cards.loader import resolve_card
        for mode in ("json", "dom"):
            card = resolve_card(str(FIXTURES / f"orders-{mode}" / "card.toml"))
            self.assertTrue(test_card(card).ok)

    def test_snapshot_reload_reparse_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dataset"
            source = Path(tmp) / "orders.json"
            source.write_bytes(JSON.encode("utf-8"))
            task = parse_card(config())
            first = api.execute(api.ExecuteRequest(dataset=str(root), json_file=str(source), request_id="once"), task)
            self.assertEqual(first.status, "completed", first.message)
            capture = revisions.list_captures(root)[0]
            self.assertEqual(revisions.read_capture(root, capture.capture_id)[1].payload, JSON)
            before_ref = revisions.list_parses(root)[0]
            before = revisions.read_parse(root, before_ref.parse_id)[1]
            self.assertEqual(before.rows[0].traces["amount"].locator["path"], "/data/orders/0/amount")
            self.assertEqual(revisions.card_from_rule(revisions.load_rule(root, before_ref.rule_id)).segment_path, task.segment_path)
            changed = replace(task, fields=[task.fields[0], replace(task.fields[1], path="/price")])
            source.unlink()
            outcome = replay.reparse(root, first.data["revision"], card=changed)
            after = revisions.read_parse(root, outcome.revision.parses[0])[1]
            self.assertEqual(after.rows[0].get("amount"), "9.99")
            self.assertEqual(outcome.diffs[0].changes[0].locator_after["path"], "/data/orders/0/price")
            revisions.activate(root, outcome.revision.revision_id)
            out = Path(tmp) / "out.csv"
            result = api.export(api.ExportRequest(dataset=str(root), out=str(out)))
            self.assertEqual(result.status, "completed", result.message)
            self.assertIn("9.99", out.read_text(encoding="utf-8-sig"))

    def test_field_rewrite_preserves_dom_segment_and_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config("dom"))
            path = Path(tmp) / "card.toml"
            card_file(path, task)
            cap = Captured(kind="html", payload=HTML)
            spec = replace(task.fields[1], sel="a", attr="href", match="first")
            patched = replace(task, fields=[task.fields[0], spec])
            expected, _ = parse_capture(cap, patched)
            _rewrite_field(str(path), spec, cap=cap, expected=expected)
            from clip2table.cards.loader import resolve_card
            loaded = resolve_card(str(path))
            self.assertEqual(loaded.segment_records, "article.order")
            self.assertEqual(loaded.fields[1].match, "first")
            self.assertEqual(parse_capture(cap, loaded)[0].rows[0].get("amount"), "/one")

    def test_cli_file_preview_and_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config())
            path, source = Path(tmp) / "card.toml", Path(tmp) / "input.json"
            source.write_bytes(JSON.encode("utf-8"))
            card_file(path, task)
            for args in (("task", "check", str(path), "--json-file", str(source), "--json"),
                         ("task", "run", "-p", str(path), "--json-file", str(source), "-d", str(Path(tmp) / "ds"), "--json")):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = main(list(args))
                self.assertEqual(result, 0, stderr.getvalue())
                self.assertEqual(json.loads(stdout.getvalue())["rows"] if args[1] == "check" else
                                 json.loads(stdout.getvalue())["counts"]["rows"], 1)

    def test_task_conversion_keeps_schema_paths_and_constraints(self):
        from clip2table.cards.loader import resolve_card
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config())
            task.required = ["id"]
            source = FIXTURES / "orders-json" / "orders.json"
            card, output = Path(tmp) / "source.toml", Path(tmp) / "task.toml"
            card_file(card, task)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["task", "new", "-p", str(card), "--json-file", str(source),
                             "--yes", "--out", str(output)])
            self.assertEqual(code, 0)
            saved = resolve_card(str(output))
            self.assertEqual(saved.schema, 2)
            self.assertEqual(saved.segment_path, "/data/orders")
            self.assertEqual(saved.required, ["id"])
            self.assertEqual(parse_capture(Captured(kind="json", payload=JSON), saved)[0].rows[0].get("amount"), "1.2300")

    def test_partial_record_coverage_prevents_complete_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config())
            raw = '{"data":{"orders":[1,{"id":2,"amount":3}]}}'
            root = Path(tmp) / "ds"
            result = api.execute(api.ExecuteRequest(dataset=str(root), capture={"kind": "json", "payload": raw}), task)
            refs = revisions.list_parses(root)
            self.assertFalse(refs[0].complete)
            self.assertEqual(len(revisions.read_parse(root, refs[0].parse_id)[1].coverage["pending"]), 1)

    def test_json_http_fetch_uses_response_type(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(JSON.encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            data = config()
            data["fetch"]["url"] = f"http://127.0.0.1:{server.server_port}/orders"
            import os
            with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}):
                rnd = harvest.harvest(parse_card(data))
            self.assertEqual(rnd.captured.kind, "json")
            self.assertEqual(parse_capture(rnd.captured, parse_card(data))[0].rows[0].get("amount"), "1.2300")
        finally:
            server.shutdown()
            server.server_close()


class TestStructureSelection(unittest.TestCase):
    def test_cli_json_pick_saves_reusable_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config())
            task.segment_path = "/missing"
            card, source = Path(tmp) / "card.toml", Path(tmp) / "data.json"
            card_file(card, task)
            source.write_bytes(JSON.encode())
            for extra in ([], ["--pick", "1"], ["--pick", "1", "--write"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(["task", "check", str(card), "--json-file", str(source), "--json", *extra])
                self.assertEqual(code, 0, out.getvalue())
                data = json.loads(out.getvalue())
                self.assertTrue(data["candidates"])
            from clip2table.cards.loader import resolve_card
            self.assertEqual(resolve_card(str(card)).segment_path, "/data/orders")

    def test_dom_anonymous_regions_save_unique_selectors(self):
        from clip2table.pipeline.structured import structure_candidates, choose_structure
        task = parse_card(config("dom"))
        task.segment_region = "section"
        cap = Captured(kind="html", payload=HTML.replace('id="orders"', '') * 2)
        candidates = structure_candidates(cap, task)
        self.assertEqual(len(candidates), 2)
        chosen = choose_structure(task, candidates, 2)
        result, _ = parse_capture(cap, chosen)
        self.assertEqual(len(result.rows), 1)
        self.assertIn("nth-of-type(2)", chosen.segment_region)

    def test_preview_ids_bind_source_and_rule(self):
        task = parse_card(config())
        before = api.preview(api.PreviewRequest(), task, Captured(kind="json", payload=JSON))
        cid = before.data["candidates"][0]["candidate_id"]
        for cap, card in ((Captured(kind="json", payload=JSON.replace('1.2300', '2.3400')), task),
                          (Captured(kind="json", payload=JSON), replace(task, required=["id"]))):
            answer = api.preview(api.PreviewRequest(candidate_id=cid), card, cap)
            self.assertEqual(answer.status, "failed")
            self.assertEqual(answer.error["code"], "invalid_action")

    def test_execute_answer_uses_saved_source_and_original_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = parse_card(config())
            task.segment_path = "/missing"
            root = str(Path(tmp) / "ds")
            first = api.execute(api.ExecuteRequest(dataset=root, request_id="start",
                capture={"kind": "json", "payload": JSON}), task)
            self.assertEqual(first.reason, "structure_selection", first.message)
            wrong = api.resume(api.ResumeRequest(dataset=root, operation_id=first.operation_id,
                request_id="wrong", checkpoint=first.checkpoint,
                action_response={"action_id": first.action["action_id"], "candidate_id": "other"}))
            self.assertEqual(wrong.error["code"], "invalid_action")
            req = api.ResumeRequest(dataset=root, operation_id=first.operation_id, request_id="answer",
                checkpoint=first.checkpoint, action_response={"action_id": first.action["action_id"],
                "candidate_id": first.action["candidates"][0]["candidate_id"]})
            final = api.resume(req)
            self.assertEqual(final.status, "completed", final.message)
            self.assertEqual(final.run_id, first.run_id)
            self.assertEqual(final.counts["rows"], 1)
            self.assertEqual(api.resume(req).outputs, final.outputs)


if __name__ == "__main__":
    unittest.main()
