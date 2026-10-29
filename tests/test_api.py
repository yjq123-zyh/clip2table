"""The program-facing entry: same operations, same counts, no stdout parsing.

These tests call `api` directly. That is the point -- if any of them had to run
the CLI and read its output, the API would not be an interface.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from clip2table import api, errors, ledger as ledger_mod, revisions
from clip2table.cards.loader import resolve_card


def _scene(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "scenes" / "order-ledger" / name


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="c2t-api-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = self.dir / "ds"
        revisions.init(self.root)
        self.card = resolve_card(str(_scene("order-ledger.toml")))

    def collect(self, page_file: str, request_id: str) -> api.OperationResult:
        return api.execute(
            api.ExecuteRequest(request_id=request_id, dataset=str(self.root),
                               text_file=str(_scene(page_file))),
            self.card)


class TestResultShape(unittest.TestCase):
    def test_every_result_carries_the_api_version(self):
        r = api.OperationResult(operation="preview")
        self.assertEqual(r.api_version, api.API_VERSION)
        self.assertIn("api_version", r.as_dict())

    def test_counts_are_ints_not_prose(self):
        r = api.OperationResult(operation="x", counts={"rows": 3})
        for v in r.counts.values():
            self.assertIsInstance(v, int)

    def test_needs_action_without_a_reason_is_rejected(self):
        with self.assertRaises(errors.InputError):
            api.OperationResult(operation="x", status=api.NEEDS_ACTION,
                                action={"what": "do something"})

    def test_needs_action_without_an_action_is_rejected(self):
        """A status that says "stop" and not what to do is a dead end."""
        with self.assertRaises(errors.InputError):
            api.OperationResult(operation="x", status=api.NEEDS_ACTION,
                                reason="login_required")

    def test_an_invented_reason_is_rejected(self):
        with self.assertRaises(errors.InputError):
            api.OperationResult(operation="x", status=api.NEEDS_ACTION,
                                reason="somethng_went_wrong",
                                action={"what": "?"})

    def test_failed_needs_an_error_code(self):
        with self.assertRaises(errors.InputError):
            api.OperationResult(operation="x", status=api.FAILED)

    def test_the_result_serialises_to_plain_json(self):
        r = api.OperationResult(operation="x", counts={"rows": 1},
                                data={"nested": {"a": [1, 2]}})
        back = json.loads(r.to_json())
        self.assertEqual(back["data"]["nested"]["a"], [1, 2])


class TestPreview(Base):
    def test_a_preview_writes_nothing(self):
        before = sorted(p.name for p in self.root.iterdir())
        r = api.preview(api.PreviewRequest(
            request_id="p1", text_file=str(_scene("input/page-1.txt"))),
            self.card)
        self.assertEqual(r.status, api.COMPLETED)
        self.assertEqual(r.counts["rows"], 6)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_a_preview_has_no_run_id(self):
        """A preview is not an observation of the business."""
        r = api.preview(api.PreviewRequest(
            request_id="p1", text_file=str(_scene("input/page-1.txt"))),
            self.card)
        self.assertEqual(r.run_id, "")

    def test_a_missing_file_is_bad_input_not_a_traceback(self):
        result = api.preview(api.PreviewRequest(request_id="p1",
                             text_file=str(self.dir / "nope.txt")), self.card)
        self.assertEqual(result.status, api.FAILED)
        self.assertEqual(result.error["code"], "not_found")

    def test_no_source_at_all_is_refused(self):
        result = api.preview(api.PreviewRequest(request_id="p1"), self.card)
        self.assertEqual(result.error["code"], "bad_input")
        self.assertIn("没有给出要解析的内容", result.message)


class TestExecute(Base):
    def test_one_page_lands_and_publishes_one_revision(self):
        r = self.collect("input/page-1.txt", "r1")
        self.assertEqual(r.status, api.COMPLETED)
        self.assertEqual(r.counts["rows"], 6)
        self.assertTrue(r.data["revision"].startswith("v-"))
        self.assertEqual(len(revisions.list_parses(self.root)), 1)
        self.assertTrue(revisions.active(self.root)["revision_id"])

    def test_an_identical_request_returns_the_first_result(self):
        """A retry is a retry. The second call must not commit a second page."""
        first = self.collect("input/page-1.txt", "same-id")
        second = self.collect("input/page-1.txt", "same-id")
        self.assertEqual(second.counts, first.counts)
        self.assertEqual(second.data["revision"], first.data["revision"])
        self.assertEqual(len(revisions.list_captures(self.root)), 1)
        self.assertEqual(len(revisions.list_revisions(self.root)), 1)
        self.assertIn("已经执行过", second.message)

    def test_the_same_id_with_different_input_is_a_conflict(self):
        """Reusing an id for another question must not answer the old one."""
        self.collect("input/page-1.txt", "shared")
        result = self.collect("input/page-2.txt", "shared")
        self.assertEqual(result.error["code"], "request_conflict")

    def test_a_second_page_adds_to_the_ledger_instead_of_replacing_it(self):
        first = self.collect("input/page-1.txt", "r1")
        second = self.collect("input/page-2.txt", "r2")
        self.assertEqual(second.counts["dataset_rows"], 11)
        rows, fields, led = api.ledger_view(self.root, baseline=first.data["revision"])
        self.assertEqual(len(rows), 11)
        self.assertEqual(sorted(s["page"] for s in led.scope), [1, 1])

    def test_the_second_run_sits_on_the_first(self):
        first = self.collect("input/page-1.txt", "r1")
        second = self.collect("input/page-2.txt", "r2")
        rev = revisions.read_revision(self.root, second.data["revision"])
        self.assertEqual(rev.parent, first.data["revision"])

    def test_a_v1_directory_is_refused_rather_than_half_read(self):
        old = self.dir / "old"
        old.mkdir()
        (old / "state.json").write_text("{}", encoding="utf-8")
        result = api.execute(api.ExecuteRequest(request_id="r1", dataset=str(old),
                             text_file=str(_scene("input/page-1.txt"))), self.card)
        self.assertEqual(result.error["code"], "legacy_layout")


class TestExport(Base):
    def test_export_writes_the_view_it_says_it_wrote(self):
        self.collect("input/page-1.txt", "r1")
        out = self.dir / "current.csv"
        r = api.export(api.ExportRequest(request_id="e1", dataset=str(self.root),
                                         view="current", format="csv",
                                         out=str(out)), _policy(self.root))
        self.assertEqual(r.status, api.COMPLETED)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("订单号", text.splitlines()[0])
        self.assertEqual(len(text.strip().splitlines()), 1 + r.counts["rows"])

    def test_export_needs_a_target(self):
        self.collect("input/page-1.txt", "r1")
        result = api.export(api.ExportRequest(request_id="e1", dataset=str(self.root)), _policy(self.root))
        self.assertEqual(result.status, api.FAILED)


class TestActionableFailures(unittest.TestCase):
    def test_a_reason_is_one_of_the_agreed_tokens(self):
        for token in errors.NEEDS_ACTION:
            e = errors.ChannelError("x", reason=token)
            self.assertTrue(e.needs_action)
        self.assertFalse(errors.ChannelError("x").needs_action)

    def test_a_challenge_wall_carries_the_verification_reason(self):
        """The reason must come from where it was seen, not from the message."""
        from clip2table import harvest
        src = Path(harvest.__file__).read_text(encoding="utf-8")
        marker = "这个页面是人机验证墙"
        at = src.index(marker)
        tail = src[at:at + 400]
        self.assertIn('reason="verification_required"', tail)

    def test_the_api_does_not_guess_a_reason_from_the_message(self):
        src = Path(api.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"验证" in e.message', src)
        self.assertNotIn('"challenge" in e.message', src)

    def test_every_reason_has_a_described_next_step(self):
        for token in errors.NEEDS_ACTION:
            act = api._action_for(token, "run-1", [{"page": 1}])
            self.assertTrue(act.get("what"))
            if token in ("verification_required", "login_required",
                         "target_confirmation", "resume_position_confirmation"):
                self.assertIn("resume_after", act)


class TestLedgerViewIsShared(Base):
    def test_the_view_the_api_returns_is_the_one_the_cli_prints(self):
        from clip2table.cli import main
        import contextlib
        import io

        self.collect("input/page-1.txt", "r1")
        rows, fields, _led = api.ledger_view(self.root, view="current")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["ledger", "-d", str(self.root), "--show", "20",
                       "-p", str(_scene("order-ledger.toml"))])
        self.assertEqual(rc, errors.OK)
        out = buf.getvalue()
        for r in rows:
            self.assertIn(str(r["订单号"]), out)
        self.assertEqual(len(rows), 6)


def _policy(root: Path) -> ledger_mod.LedgerPolicy:
    """The policy a caller would use when it has no card handy.

    `api` reads the key list out of the rule snapshot the revision was built
    with, so this is the honest default for a program that only knows its
    dataset path.
    """
    rid = revisions.active(root)["revision_id"]
    return ledger_mod.LedgerPolicy(keys=api._keys_for(root, rid))


if __name__ == "__main__":
    unittest.main()
