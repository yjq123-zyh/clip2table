"""Incremental publication, bounded queries and recoverable indexed export."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip2table import api, index, revisions as R
from clip2table.models import Captured, Row
from clip2table.render.xlsx import write_xlsx
from tests.test_structured import config
from clip2table.cards.schema import parse_card


class TestIncremental(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dataset"
        self.card = parse_card(config())

    def collect(self, rows, request_id):
        result = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id=request_id,
            capture={"kind": "json", "payload": json.dumps({"data": {"orders": rows}})}), self.card)
        self.assertEqual(result.status, "completed", result.message)
        return result

    def test_append_reads_only_new_parse_bodies(self):
        first = self.collect([{"id": 1, "amount": "10"}], "first")
        old = set(R.read_revision(self.root, first.outputs["revision"]).parses)
        read = R.read_parse
        def checked(root, identity):
            self.assertNotIn(identity, old)
            return read(root, identity)
        with patch.object(R, "read_parse", side_effect=checked):
            second = self.collect([{"id": 2, "amount": "20"}], "second")
        body = R._load(self.root / R.REVISIONS / (second.outputs["revision"] + ".json"), {})
        self.assertNotIn("entries", body)
        self.assertEqual((body["entry_count"], len(body["delta"])), (2, 1))
        self.assertEqual(second.counts["dataset_rows"], 2)

    def test_query_cache_rebuild_and_all_views_match_reference(self):
        first = self.collect([{"id": 1, "amount": "10"}, {"id": 2, "amount": "20"}], "first")
        second = self.collect([{"id": 1, "amount": "12"}, {"id": 3, "amount": "30"}], "second")
        for view in ("current", "history", "changes", "pending"):
            actual = api.query_view(self.root, baseline=first.outputs["revision"], view=view)
            expected = api.ledger_view(self.root, baseline=first.outputs["revision"], view=view)
            self.assertEqual(actual[:2], expected[:2], view)
        with patch.object(R, "read_parse", side_effect=AssertionError("historical body")), \
             patch.object(R, "read_capture", side_effect=AssertionError("original body")):
            self.assertEqual(len(api.query_view(self.root, limit=2)[0]), 2)
        index.rebuild(self.root)
        self.assertEqual(len(api.query_view(self.root)[0]), 3)
        for view in ("current", "history", "changes", "pending"):
            self.assertEqual(api.query_view(self.root, baseline=first.outputs["revision"], view=view)[:2],
                             api.ledger_view(self.root, baseline=first.outputs["revision"], view=view)[:2])
        self.assertEqual(R.active(self.root)["revision_id"], second.outputs["revision"])

    def test_run_header_contains_counters_and_page_references(self):
        result = self.collect([{"id": 1, "amount": "10"}], "first")
        header = R._load(self.root / R.RUNS / (result.run_id + ".json"), {})
        self.assertNotIn("entries", header)
        self.assertNotIn("captures", header)
        self.assertEqual(header["rows"], 1)
        load = R._load
        def bounded(path, default):
            self.assertTrue(Path(path).parent.name != result.run_id or Path(path).name == "snapshot.json")
            return load(path, default)
        with patch.object(R, "_load", side_effect=bounded):
            self.assertEqual(len(R.read_run(self.root, result.run_id).entries), 1)

    def test_export_state_is_a_query_and_loss_reference(self):
        self.collect([{"id": 1, "amount": "x" * 33000}], "first")
        output = Path(self.temp.name) / "result.xlsx"
        request = api.ExportRequest(dataset=str(self.root), out=str(output), format="xlsx", request_id="export")
        result = api.export(request)
        self.assertEqual(result.status, "completed", result.message)
        self.assertEqual(len(result.data["render_changes"]), 1)
        state = R._load(self.root / "operation-state" / (result.operation_id + ".json"), {})
        self.assertNotIn("rows", state["export"])
        self.assertNotIn("unparsed", state["export"])
        self.assertLess(len(json.dumps(state)), 12000)
        self.assertEqual(api.export(request).data["render_changes"], result.data["render_changes"])
        self.assertEqual(len(json.loads(Path(result.outputs["companion"]).read_text(encoding="utf-8"))["value"]), 33000)

    def test_adoption_preserves_requests_and_pending_capture(self):
        self.card.segment_path = "/missing"
        first = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id="first",
            capture={"kind": "json", "payload": '{"data":{"orders":[{"id":1,"amount":"10"}]}}'}), self.card)
        target = Path(self.temp.name) / "adopted"
        R.adopt_dataset(self.root, target)
        original = api.execute(api.ExecuteRequest(dataset=str(target), request_id="first",
            capture={"kind": "json", "payload": '{"data":{"orders":[{"id":1,"amount":"10"}]}}'}), self.card)
        self.assertEqual(original.operation_id, first.operation_id)
        final = api.resume(api.ResumeRequest(dataset=str(target), operation_id=first.operation_id,
            request_id="continue", checkpoint=first.checkpoint, action_response={
                "action_id": first.action["action_id"], "candidate_id": first.action["candidates"][0]["candidate_id"]}))
        self.assertEqual(final.status, "completed", final.message)
        self.assertEqual(len(api.query_view(target)[0]), 1)

    def test_writer_consumes_generator_once(self):
        import zipfile
        seen = []
        def rows():
            for n in range(250):
                seen.append(n)
                yield Row(values={"n": n})
        path = Path(self.temp.name) / "stream.xlsx"
        result = write_xlsx(rows(), ["n"], path, detail="summary")
        self.assertTrue(result.lossless)
        self.assertEqual(len(seen), 250)
        with zipfile.ZipFile(path) as archive:
            self.assertIn(b'A1:A251', archive.read("xl/worksheets/sheet1.xml"))

    def test_publication_crash_reuses_immutable_objects(self):
        for kind in ("capture", "parse", "revision", "selection"):
            with self.subTest(kind=kind):
                self.root = Path(self.temp.name) / kind
                record = index.record
                def interrupt(root, item_kind, identity, body):
                    if item_kind == kind:
                        raise OSError("simulated interruption")
                    return record(root, item_kind, identity, body)
                with patch.object(index, "record", side_effect=interrupt):
                    failed = api.execute(api.ExecuteRequest(dataset=str(self.root), request_id="first",
                        capture={"kind": "json", "payload": '{"data":{"orders":[{"id":1,"amount":"10"}]}}'}), self.card)
                self.assertEqual(failed.status, api.FAILED)
                capture = R.list_captures(self.root)[0]
                with patch.object(api, "_capture", side_effect=AssertionError("live source fetched again")):
                    final = api.resume(api.ResumeRequest(dataset=str(self.root), request_id="retry",
                        operation_id=failed.operation_id, checkpoint=failed.checkpoint))
                self.assertEqual(final.status, api.COMPLETED, final.to_json())
                self.assertEqual((final.counts["pages"], final.counts["rows"]), (1, 1))
                self.assertEqual(R.list_captures(self.root)[0].capture_id, capture.capture_id)
                self.assertEqual(len(api.query_view(self.root)[0]), 1)

    def test_page_event_recovers_count_before_header_write(self):
        final = self.collect([{"id": 1, "amount": "10"}], "first")
        path = self.root / R.RUNS / (final.run_id + ".json")
        header = R._load(path, {})
        header["rows"] = 0
        header.pop("snapshot", None)
        R._dump(header, path)
        self.assertEqual(R.read_run(self.root, final.run_id).rows, 1)

    def test_corrected_partial_revision_recovers_quality(self):
        from dataclasses import replace
        from clip2table.core import parse_capture
        R.init(self.root)
        cap = Captured(kind="json", payload='{"data":{"orders":[null,{"id":1,"amount":"10"}]}}')
        cref = R.write_capture(self.root, cap, run_id="quality", page=1)
        result, _ = parse_capture(cap, self.card)
        pref = R.write_parse(self.root, cref, R.write_rule(self.root, self.card), result)
        partial = R.write_revision(self.root, [(cref.capture_id, pref.parse_id)])
        self.assertFalse(partial.quality_complete)
        fixed = replace(self.card, fields=[replace(f, type="text") for f in self.card.fields])
        result.coverage = {"candidates": 1, "parsed": 1, "pending": []}
        corrected = R.write_parse(self.root, cref, R.write_rule(self.root, fixed), result)
        final = R.write_revision(self.root, R.carry_forward(self.root, partial.revision_id,
            [(cref.capture_id, corrected.parse_id)]), parent=partial.revision_id)
        self.assertTrue(final.complete)

    def test_stale_index_replays_committed_events(self):
        import shutil
        self.collect([{"id": 1, "amount": "10"}], "first")
        database = self.root / "views" / "index.sqlite3"
        backup = Path(self.temp.name) / "index-backup.sqlite3"
        shutil.copy2(database, backup)
        self.collect([{"id": 2, "amount": "20"}], "second")
        shutil.copy2(backup, database)
        rows, _, _ = api.query_view(self.root)
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        with index.database(self.root) as db:
            sequence = int(db.execute("SELECT value FROM settings WHERE key='commit_seq'").fetchone()[0])
        self.assertEqual(sequence, R._load(self.root / "events" / "state.json", {})["commit_seq"])

    def test_adoption_copies_unfinished_export_artifacts(self):
        self.collect([{"id": 1, "amount": "x" * 33000}], "first")
        request = api.ExportRequest(dataset=str(self.root), format="xlsx", out=str(Path(self.temp.name) / "result.xlsx"))
        with patch.object(api, "_publish_export", side_effect=OSError("interrupted before publication")):
            failed = api.export(request)
        state = R._load(api._state_path(self.root, failed.operation_id), {})
        target = Path(self.temp.name) / "adopted-export"
        R.adopt_dataset(self.root, target)
        for artifact in state["export"]["artifacts"]:
            Path(artifact["stage"]).unlink()
        final = api.resume(api.ResumeRequest(dataset=str(target), operation_id=failed.operation_id,
            checkpoint=failed.checkpoint))
        self.assertTrue(final.ok, final.to_json())
        self.assertEqual(final.counts["rows"], 1)
        self.assertEqual(len(final.data["render_changes"]), 1)

    def test_decision_recomputes_only_its_entity(self):
        from clip2table import ledger
        self.card.mutable_fields = []
        first = self.collect([{"id": n, "amount": "10"} for n in range(20)], "first")
        self.collect([{"id": 1, "amount": "20"}], "second")
        pending = api.ledger_view(self.root)[2].pending[0]
        action = api.resolve(api.Request(dataset=str(self.root)), pending_id=pending.pending_id)
        choice = next(item["candidate_id"] for item in pending.candidates if item["values"]["amount"] == "20")
        answer = api.resume(api.ResumeRequest(dataset=str(self.root), operation_id=action.operation_id,
            checkpoint=action.checkpoint, action_response={"action_id": action.action["action_id"], "candidate": choice}))
        self.assertTrue(answer.ok, answer.to_json())
        with patch.object(ledger, "build", wraps=ledger.build) as build:
            actual = api.query_view(self.root)[0]
        self.assertEqual(build.call_count, 1)
        self.assertEqual({row["id"]: row["amount"] for row in actual}["1"], "20")
        self.assertEqual(api.query_view(self.root, view="pending")[0], [])
        self.assertEqual(len(api.query_view(self.root, view="pending", decision_set_id="")[0]), 1)
        for view in ("current", "history", "changes", "pending"):
            indexed = api.query_view(self.root, view=view, baseline=first.outputs["revision"])
            expected = api.ledger_view(self.root, view=view, baseline=first.outputs["revision"])
            self.assertEqual(indexed[:2], expected[:2], view)


if __name__ == "__main__":
    unittest.main()
