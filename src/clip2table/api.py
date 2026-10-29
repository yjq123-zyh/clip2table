"""Synchronous application operations with versioned results and durable recovery.

Requests bind their actual inputs before writing. Operations freeze configuration,
keep committed progress, and persist receipts for retries. CLI adapters call the
same collection, replay, selection and export operations.
"""
from __future__ import annotations

import dataclasses
import json
import time
import hashlib
import uuid
import os
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from . import ledger as ledger_mod
from . import replay as replay_mod
from . import revisions
from .cards.schema import Card
from .errors import Clip2TableError, InputError
from .models import Captured

API_VERSION = 1

# The four ways an operation can end.
COMPLETED = "completed"
NEEDS_ACTION = "needs_action"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES = (COMPLETED, NEEDS_ACTION, FAILED, CANCELLED)

# Why a human is needed. Stable tokens on purpose: a program switches on these,
# a human reads `message`.
ACTION_REASONS = (
    "structure_selection",          # the reading is ambiguous, pick one
    "target_confirmation",          # which window/tab is the source?
    "login_required",               # the site wants credentials, in the user's browser
    "verification_required",        # captcha / slider / interstitial
    "resume_position_confirmation",  # cannot prove which page we are on
    "conflict_resolution",          # two observations disagree, someone must choose
    "revision_selection",           # a candidate revision is ready, activate it?
    "legacy_capture_type_required",  # old page has no kind; text or html?
)

# Error codes for `failed`. Kept small and stable like the CLI exit codes.
E_CODES = ("bad_input", "channel_unavailable", "empty_capture", "not_found",
           "incomplete_result", "internal")


@dataclass
class OperationResult:
    """The only thing an operation returns.

    `counts` never carries prose. A field that is sometimes a number and
    sometimes a sentence is not data, and a caller that has to test its type
    eventually tests it wrong.
    """

    operation: str = ""
    api_version: int = API_VERSION
    operation_id: str = ""
    request_id: str = ""
    run_id: str = ""
    status: str = COMPLETED
    reason: str = ""
    message: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise InputError(f"未知状态 {self.status!r}，只能是 {list(STATUSES)}")
        if self.status == NEEDS_ACTION:
            if self.reason not in ACTION_REASONS:
                raise InputError(
                    f"needs_action 的 reason 必须是 {list(ACTION_REASONS)} 之一，"
                    f"不是 {self.reason!r}")
            if not self.action:
                # A needs_action without a described next step is a dead end
                # dressed up as a status: the caller knows it must stop and has
                # no way to tell the human what to do.
                raise InputError("needs_action 必须带 action（说明下一步做什么）")
        if self.status == FAILED and not self.error.get("code"):
            raise InputError("failed 必须带 error.code")

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=indent)


# ------------------------------------------------------------------ requests


@dataclass
class Request:
    """Common fields. Supply request_id to identify retries of a write.

    Two retries of the same logical request carry the same id; the second one
    returns the first one's result instead of committing again. Without it a
    network timeout becomes a duplicated page, which is the one failure mode a
    ledger cannot undo.
    """

    request_id: str = ""
    dataset: str = ""
    card: str = ""
    api_version: int = API_VERSION

    def fingerprint(self) -> str:
        """A digest of everything that changes the outcome, minus the id."""
        body = {k: v for k, v in self.__dict__.items() if k != "request_id"}
        return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class PreviewRequest(Request):
    text_file: str = ""
    html_file: str = ""
    url: str = ""
    rows: int = 5


@dataclass
class ExecuteRequest(Request):
    text_file: str = ""
    html_file: str = ""
    url: str = ""
    max_pages: int | None = None
    port: int | None = None
    allow_card_change: bool = False
    append_from_here: bool = False
    expected_active_token: dict[str, Any] | None = None
    allow_force: bool = False
    capture: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResumeRequest(Request):
    run_id: str = ""
    max_pages: int | None = None
    port: int | None = None
    operation_id: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    action_response: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReparseRequest(Request):
    base: str = ""
    rule: str = ""
    captures: list[str] = field(default_factory=list)
    select: bool = False


@dataclass
class CompareRequest(Request):
    left: str = ""
    right: str = ""


@dataclass
class SelectRequest(Request):
    revision: str = ""
    expect: str = ""
    allow_incomplete: bool = False
    expected_active_token: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    decision_set_id: str = ""


@dataclass
class ExportRequest(Request):
    revision: str = ""
    baseline: str = ""
    view: str = "current"
    format: str = "csv"
    out: str = ""
    overwrite: bool = False
    decision_set_id: str | None = None
    include_metadata: bool = True
    baseline_decision_set_id: str = ""


# ------------------------------------------------------------------- durable operations

def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _result(body) -> OperationResult:
    known = {f.name for f in dataclasses.fields(OperationResult)}
    return OperationResult(**{k: v for k, v in body.items() if k in known})


def _state_path(root: Path, oid: str) -> Path:
    return root / "operation-state" / f"{oid}.json"


def _receipt_path(root: Path, rid: str) -> Path:
    return root / "operations" / f"{_digest(rid)}.json"


def _save(root: Path, state: dict) -> None:
    revisions._dump(state, _state_path(root, state["operation_id"]))


def _fingerprint(operation, req, card=None, policy=None, extra=None) -> str:
    body = dataclasses.asdict(req)
    body.pop("request_id", None)
    content = {}
    for key in ("text_file", "html_file"):
        filename = body.get(key)
        if filename:
            path = Path(filename)
            if not path.is_file():
                raise InputError(f"找不到文件：{path}", code="not_found")
            content[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    if card is not None:
        from .dataset import _card_fingerprint
        content["card"] = _card_fingerprint(card)
    if policy is not None:
        content["policy"] = policy.as_dict()
    return _digest([operation, body, content, extra or {}])


def _check_version(req) -> None:
    if req.api_version != API_VERSION:
        raise InputError(f"不支持 API 版本 {req.api_version}", code="unsupported_version")


def _failure(operation, req, error, state=None) -> OperationResult:
    state = state or {}
    checkpoint = dict(state.get("checkpoint") or {})
    counts = dict(state.get("counts") or {})
    reason = error.reason if isinstance(error, Clip2TableError) else ""
    status = NEEDS_ACTION if reason in ACTION_REASONS else FAILED
    action = {}
    if status == NEEDS_ACTION:
        action = _action_for(reason, state.get("run_id", ""), [])
        action.update(action_id="action-" + _digest([state.get("operation_id"), checkpoint, reason])[:20],
                      reason=reason,
                      operation_id=state.get("operation_id", ""),
                      checkpoint=checkpoint, response_via="resume")
        action.update(getattr(error, "details", {}))
    return OperationResult(
        operation=operation, operation_id=state.get("operation_id", req.request_id),
        run_id=state.get("run_id", ""), status=status, reason=reason,
        message=str(error), counts=counts, action=action, checkpoint=checkpoint,
        outputs=dict(state.get("outputs") or {}),
        error={} if action else {"code": getattr(error, "code", "io_error"),
                                  "details": getattr(error, "details", {}),
                                  "hint": getattr(error, "hint", ""),
                                  "retryable": not isinstance(error, InputError)})


def _entry(operation: str, *, write: bool):
    """Shared error boundary, request binding, writer lock and durable receipts."""
    def decorate(fn):
        @wraps(fn)
        def call(req, *args, **kwargs):
            state = None
            root = Path(req.dataset) if req.dataset else None
            try:
                _check_version(req)
                if not req.request_id:
                    req.request_id = uuid.uuid4().hex
                if not write:
                    out = fn(req, *args, **kwargs)
                    out.operation_id = out.operation_id or req.request_id
                    out.request_id = req.request_id
                    return out
                if root is None:
                    raise InputError("需要 dataset", code="bad_input")
                if operation == "execute" and revisions.layout_of(root) == 0:
                    revisions.init(root)
                _v2(root)
                with revisions.writer(root):
                    supplied_card = kwargs.get("card") or (args[0] if args and isinstance(args[0], Card) else None)
                    if operation in ("execute", "reparse") and supplied_card is None and req.card:
                        from .cards.loader import resolve_card
                        supplied_card = resolve_card(req.card)
                        if args:
                            args = (supplied_card,) + args[1:]
                        else:
                            kwargs["card"] = supplied_card
                    supplied_policy = kwargs.get("policy") or (args[0] if args and isinstance(args[0], ledger_mod.LedgerPolicy) else None)
                    fp = _fingerprint(operation, req, supplied_card, supplied_policy,
                                      {k: v for k, v in kwargs.items() if k not in ("card", "policy")})
                    rp = _receipt_path(root, req.request_id)
                    receipt = revisions._load(rp, None)
                    if receipt:
                        if receipt["fingerprint"] != fp:
                            raise InputError("这个 request_id 的输入不一样", code="request_conflict")
                        out = _result(receipt["result"])
                        out.message = "这次请求已经执行过，返回原结果"
                        return out
                    oid = "op-" + _digest([operation, req.request_id])[:24]
                    if operation == "resume":
                        oid = req.operation_id or (
                            revisions.read_run(root, req.run_id).operation_id if req.run_id else "")
                        if not oid:
                            raise InputError("resume 需要 operation_id 或 run_id", code="bad_input")
                    state = revisions._load(_state_path(root, oid), None)
                    if state is None and operation == "resume":
                        raise InputError("没有这个可恢复操作", code="not_found")
                    state = state or {"operation_id": oid, "operation": operation,
                                      "request_id": req.request_id, "fingerprint": fp,
                                      "request": dataclasses.asdict(req), "results": {},
                                      "generation": 0, "counts": {}, "checkpoint": {}}
                    saved = state["results"].get(req.request_id)
                    if saved:
                        if saved["fingerprint"] != fp:
                            raise InputError("这个 request_id 的输入不一样", code="request_conflict")
                        revisions._dump(saved, rp)
                        return _result(saved["result"])
                    bindings = state.setdefault("bindings", {})
                    if req.request_id in bindings and bindings[req.request_id] != fp:
                        raise InputError("这个 request_id 的输入不一样", code="request_conflict")
                    bindings[req.request_id] = fp
                    _save(root, state)
                    prior_counts = dict(state.get("counts") or {})
                    transition = True
                    try:
                        out = fn(req, *args, _operation=state, **kwargs)
                    except KeyboardInterrupt:
                        out = OperationResult(operation=operation, status=CANCELLED,
                                              reason="user_cancelled", message="已取消",
                                              counts=state.get("counts", {}),
                                              checkpoint=state.get("checkpoint", {}),
                                              outputs=state.get("outputs", {}))
                    except (Clip2TableError, OSError) as error:
                        out = _failure(operation, req, error, state)
                        transition = getattr(error, "code", "") not in (
                            "invalid_action", "stale_checkpoint", "operation_completed", "revision_conflict")
                    out.operation_id = oid
                    out.request_id = req.request_id
                    out.run_id = out.run_id or state.get("run_id", "")
                    if out.run_id:
                        out.counts["this_request_pages"] = max(0, out.counts.get("pages", 0) - prior_counts.get("pages", 0))
                        out.counts["this_request_rows"] = max(0, out.counts.get("rows", 0) - prior_counts.get("rows", 0))
                    if transition:
                        state["generation"] += 1
                    out.checkpoint = dict(out.checkpoint or state.get("checkpoint") or {})
                    out.checkpoint.update(operation_id=oid, generation=state["generation"])
                    if transition:
                        state.update(checkpoint=out.checkpoint, counts=out.counts,
                                     outputs=out.outputs, action=out.action, status=out.status)
                    saved = {"fingerprint": fp, "result": out.as_dict()}
                    state["results"][req.request_id] = saved
                    _save(root, state)  # committed outcome survives a missing receipt
                    revisions._dump(saved, rp)
                    if transition:
                        _update_run(root, state, out)
                    return out
            except (Clip2TableError, OSError) as error:
                return _failure(operation, req, error, state)
        return call
    return decorate


def _update_run(root, state, out):
    if state.get("run_id"):
        run = revisions.read_run(root, state["run_id"])
        run.status = out.status
        run.checkpoint.update(out.checkpoint)
        revisions.save_run(root, run)


def _require(root: Path) -> None:
    if not root.is_dir():
        raise InputError(f"没有这个目录：{root}", code="not_found")


def _v2(root: Path, what: str = "") -> None:
    _require(root)
    if revisions.layout_of(root) != 2:
        raise InputError(f"{root} 还是旧布局，请显式采用旧页面后再使用版本接口",
                         code="legacy_layout")
    revisions.init(root)


def _capture(req) -> Captured:
    if getattr(req, "capture", None):
        return Captured(**req.capture)
    for name, kind in (("text_file", "text"), ("html_file", "html")):
        filename = getattr(req, name, "")
        if filename:
            path = Path(filename)
            if not path.is_file():
                raise InputError(f"找不到文件：{path}", code="not_found")
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeError:
                raise InputError(f"文件不是 UTF-8：{path}", code="invalid_encoding") from None
            return Captured(kind=kind, payload=text,
                            channel="file", formats=["file"])
    if getattr(req, "url", ""):
        from . import ladder
        return ladder.capture(req.url, "auto")[0]
    raise InputError("没有给出要解析的内容", code="bad_input")


def _action_for(reason, run_id, outcomes):
    labels = {
        "verification_required": "在来源页面完成验证后继续",
        "login_required": "在来源页面完成登录后继续",
        "target_confirmation": "确认目标窗口或标签页",
        "resume_position_confirmation": "把来源定位到检查点的下一页并确认",
        "structure_selection": "选择结构，保存规则并重新预览",
        "conflict_resolution": "选择已有候选或保持待处理",
        "revision_selection": "比较候选后显式选择结果版本",
        "legacy_capture_type_required": "通过 rev adopt 指定旧页面的类型",
    }
    via = {"revision_selection": "select_revision", "structure_selection": "task check/task fix",
           "legacy_capture_type_required": "rev adopt"}.get(reason, "resume")
    return {"what": labels.get(reason, reason), "response_via": via,
            "resume_after": {"run_id": run_id, "page": len(outcomes) + 1},
            "input": {"confirmed": "bool"}}


@_entry("preview", write=False)
def preview(req: PreviewRequest, card=None, capture=None) -> OperationResult:
    from .core import parse_capture
    if card is None and req.card:
        from .cards.loader import resolve_card
        card = resolve_card(req.card)
    cap = capture or _capture(req)
    res, inf = parse_capture(cap, card)
    candidates = []
    if inf:
        candidates = [dict(index=i, **dataclasses.asdict(c)) for i, c in enumerate(inf.alternates, 1)]
    return OperationResult(operation="preview", counts={"rows": len(res.rows),
                           "fields": len(res.fields), "unparsed": len(res.unparsed)},
                           quality={"level": res.level, "fill": res.fill_rates,
                                    "conservation": res.check_conservation()},
                           data={"fields": res.fields,
                                 "rows": [revisions._row_record(r) for r in res.rows],
                                 "unparsed": res.unparsed[:50], "candidates": candidates})


def default_policy(root, rid):
    selected = revisions.active(root)
    if selected.get("revision_id") == rid and selected.get("policy"):
        return ledger_mod.LedgerPolicy.from_dict(selected["policy"])
    rev = revisions.read_revision(root, rid)
    if rev.policy:
        return ledger_mod.LedgerPolicy.from_dict(rev.policy)
    for entry in rev.entries:
        pref, _ = revisions.read_parse(root, entry["parse_id"])
        if pref.legacy_policy:
            return ledger_mod.LedgerPolicy.from_dict(pref.legacy_policy)
        snap = revisions.load_rule(root, pref.rule_id)
        card = revisions.card_from_rule(snap)
        if card.dedupe_key:
            return ledger_mod.policy_from_card(card)
    raise InputError("这个版本没有主键，无法建立台账", code="missing_key")


def _keys_for(root, rid):
    return default_policy(root, rid).keys


def _next_page(root, card):
    return 1


def _dataset_rows(root):
    rid = revisions.active(root).get("revision_id")
    return ledger_view(root, rid)[0] if rid else []


def _run_checkpoint(root, state, run):
    state["run_id"] = run.run_id
    state["checkpoint"] = dict(run.checkpoint, run_id=run.run_id)
    state["counts"] = {"pages": len(run.entries),
                       "captured_pages": len(run.captures),
                       "committed": len(run.entries),
                       "rows": sum(revisions.read_parse(root, e["parse_id"])[0].rows for e in run.entries)}
    _save(root, state)


def _recover_pages(root, pub):
    """A capture published before the progress file is enough to finish that page."""
    from .core import parse_capture
    known = {c for c, _ in pub.entries}
    for cref in sorted((c for c in revisions.list_captures(root) if c.run_id == pub.run.run_id),
                       key=lambda c: c.page):
        if cref.capture_id in known:
            continue
        _, cap = revisions.read_capture(root, cref.capture_id)
        res, _ = parse_capture(cap, pub.card)
        pub.page(cref.page, cap, res)
    return max((revisions.read_capture(root, c)[0].page for c, _ in pub.entries), default=0) + 1


def _collect(req, card, state, root, *, continuing=False):
    from .core import parse_capture
    from .harvest import harvest_pages, can_restore
    if "frozen" not in state:
        if card is None:
            from .cards.loader import resolve_card
            card = resolve_card(req.card)
        base = revisions.active(root).get("revision_id") or ""
        token = getattr(req, "expected_active_token", None)
        if token is not None and token != revisions.active_token(root):
            raise InputError("当前选择已改变", code="revision_conflict")
        state["frozen"] = {"card": revisions.card_config(card), "base": base,
                           "active_token": token or revisions.active_token(root),
                           "request": dataclasses.asdict(req)}
        state["run_id"] = "run-" + _digest(state["operation_id"])[:20]
        _save(root, state)
    frozen = state["frozen"]
    card = revisions.restore_card(frozen["card"])
    if state.get("resolved_target"):
        card.harvest["target_id"] = state["resolved_target"]
    config = frozen["request"]
    run_file = root / revisions.RUNS / f"{state['run_id']}.json"
    run = revisions.read_run(root, state["run_id"]) if run_file.is_file() else revisions.new_run(
        root, task=card.name, base_revision=frozen["base"], config=frozen,
        operation_id=state["operation_id"], run_id=state["run_id"])
    pub = revisions.Publisher(root, card, run=run, base_revision=frozen["base"])
    try:
        start = _recover_pages(root, pub)
    finally:
        _run_checkpoint(root, state, pub.run)
    is_file = bool(config.get("text_file") or config.get("html_file") or config.get("url") or config.get("capture"))
    limit = config.get("max_pages") if config.get("max_pages") is not None else card.max_pages
    limit = max(1, int(limit))
    if is_file:
        if not pub.entries:
            cap = _capture(ExecuteRequest(**{k: v for k, v in config.items()
                                            if k in ExecuteRequest.__dataclass_fields__}))
            pub.page(1, cap, None)
            _run_checkpoint(root, state, run)
            res, _ = parse_capture(cap, card)
            pub.page(1, cap, res)
        stop = "scope_limit_reached"
    else:
        remaining = max(0, limit - len(pub.entries))
        if continuing and getattr(req, "max_pages", None) is not None:
            remaining = max(0, min(remaining, req.max_pages))
        if remaining:
            positioned = False
            if start > 1 and not can_restore(card)[0]:
                response = getattr(req, "action_response", {})
                if not response.get("confirmed"):
                    raise InputError("需要确认来源已定位到下一页",
                                     reason="resume_position_confirmation")
                positioned = True
            def capture_page(rnd):
                pub.page(rnd.page, rnd.captured, None)
                _run_checkpoint(root, state, run)
            def land(rnd):
                pub.page(rnd.page, rnd.captured, rnd.result)
                _run_checkpoint(root, state, run)
            try:
                multi = harvest_pages(card, max_pages=remaining,
                                      port=config.get("port"), allow_force=config.get("allow_force", False),
                                      start_page=start, on_capture=capture_page, on_page=land,
                                      positioned=positioned)
                stop = multi.stopped_code or "scope_limit_reached"
            finally:
                _run_checkpoint(root, state, run)
        else:
            stop = state.get("stop", "scope_limit_reached")
    state["stop"] = stop
    _run_checkpoint(root, state, run)
    entries = revisions.carry_forward(root, frozen["base"], pub.entries)
    if not entries:
        run.status = COMPLETED
        revisions.save_run(root, run)
        return OperationResult(operation="execute", run_id=run.run_id, counts=state["counts"],
                               reason=stop, checkpoint=state["checkpoint"])
    rev = revisions.write_revision(root, entries, parent=frozen["base"] or None,
                                   reason="execute: " + run.run_id,
                                   policy=ledger_mod.policy_from_card(card).as_dict())
    state["outputs"] = {"dataset": str(root), "revision": rev.revision_id}
    if rev.complete and revisions.active(root).get("operation_id") != state["operation_id"]:
        revisions.activate(root, rev.revision_id, expected_token=frozen["active_token"],
                           operation_id=state["operation_id"])
    counts = dict(state["counts"])
    if not card.dedupe_key:
        counts["dataset_rows"] = sum(revisions.read_parse(root, e["parse_id"])[0].rows for e in rev.entries)
        return OperationResult(operation="execute", run_id=run.run_id, reason=stop,
                               outputs=state["outputs"], counts=counts,
                               checkpoint=run.checkpoint, data={"revision": rev.revision_id})
    led = ledger_mod.build(root, rev.revision_id, ledger_mod.policy_from_card(card),
                           baseline=frozen["base"])
    counts.update(dataset_rows=len(led.current), pending=len(led.pending))
    counts.update(new=sum(c.kind == "new" for c in led.changes),
                  updated=sum(c.kind == "updated" for c in led.changes))
    if not frozen["base"]:
        counts["new"] = len(led.current) - len([c for c in led.current if c.pending])
    changed_keys = {c.label for c in led.changes} | {c.label for c in led.pending}
    counts["unchanged"] = len([c for c in led.current if c.label not in changed_keys]) if frozen["base"] else 0
    run.status = COMPLETED
    run.checkpoint.update(revision=rev.revision_id, next_page=start if is_file else len(run.entries) + 1)
    revisions.save_run(root, run)
    return OperationResult(operation="execute", run_id=run.run_id, reason=stop,
                           outputs=state["outputs"], counts=counts,
                           quality={"complete": rev.complete, "quality_complete": rev.quality_complete,
                                    "operation_complete": rev.operation_complete},
                           checkpoint=run.checkpoint,
                           data={"revision": rev.revision_id, "pages": run.entries})


@_entry("execute", write=True)
def execute(req: ExecuteRequest, card=None, *, _operation=None):
    return _collect(req, card, _operation, Path(req.dataset))


def _validate_resume(req, state):
    cp = req.checkpoint
    if cp and (cp.get("generation") != state.get("generation")
               or cp.get("operation_id") != state["operation_id"]):
        raise InputError("检查点已过期，请读取最新状态", code="stale_checkpoint")
    action = state.get("action") or {}
    if action:
        response = req.action_response
        if response.get("action_id") != action.get("action_id"):
            raise InputError("动作回答不属于当前操作", code="invalid_action")
        if action.get("response_via") != "resume":
            raise InputError(f"这个动作通过 {action.get('response_via')} 处理", code="invalid_action")
        if not cp:
            raise InputError("回答动作需要 checkpoint", code="stale_checkpoint")
        if action.get("reason") in ("login_required", "verification_required", "resume_position_confirmation") and response.get("confirmed") is not True:
            raise InputError("请在完成来源操作后确认", code="invalid_action")


@_entry("resume", write=True)
def resume(req: ResumeRequest, card=None, *, _operation=None):
    state = _operation
    root = Path(req.dataset)
    _validate_resume(req, state)
    if state.get("action", {}).get("reason") == "target_confirmation" and state["action"].get("candidates"):
        target = req.action_response.get("target_id")
        if target not in {c.get("id") for c in state["action"]["candidates"]}:
            raise InputError("请选择动作列出的目标", code="invalid_action")
        state["resolved_target"] = target
    if state.get("status") == COMPLETED and state.get("operation") != "resolve":
        raise InputError("操作已完成；再次采集应创建新运行", code="operation_completed")
    if state.get("operation") == "reparse":
        frozen = state["reparse"]
        sub = ReparseRequest(dataset=str(root), base=frozen["base"], rule=frozen["rule"],
                             captures=list(frozen.get("failed") or frozen["captures"]))
        return _reparse(sub, None, state)
    if state.get("operation") == "resolve":
        return _resolve_answer(root, req, state)
    if state.get("operation") == "export":
        sub = ExportRequest(**state["request"])
        return export.__wrapped__(sub, policy=ledger_mod.LedgerPolicy.from_dict(state["export"]["policy"]),
                                  _operation=state)
    return _collect(req, None, state, root, continuing=True)


def _reparse(req, card, state):
    root = Path(req.dataset)
    if "reparse" not in state:
        if card is None and req.card:
            from .cards.loader import resolve_card
            card = resolve_card(req.card)
        base = req.base or revisions.active(root).get("revision_id") or ""
        if not base:
            raise InputError("没有重解析的基线版本")
        rule = replay_mod.rule_for(root, card=card, rule_id=req.rule,
                                   base=revisions.read_revision(root, base))
        caps = req.captures or revisions.read_revision(root, base).captures
        state["reparse"] = {"base": base, "rule": rule.rule_id, "captures": caps,
                            "active_token": revisions.active_token(root)}
        _save(root, state)
    frozen = state["reparse"]
    rep = replay_mod.reparse(root, frozen.get("candidate") or frozen["base"],
                             rule_id=frozen["rule"],
                             captures=frozen.get("failed") or frozen["captures"], reason="reparse")
    frozen.update(candidate=rep.revision.revision_id,
                  failed=[f["capture_id"] for f in rep.failed])
    _save(root, state)
    if req.select and not rep.failed:
        revisions.activate(root, rep.revision.revision_id,
                           expected_token=frozen["active_token"], operation_id=state["operation_id"])
    return OperationResult(
        operation="reparse", status=FAILED if rep.failed else COMPLETED,
        error={"code": "reparse_failed", "retryable": True} if rep.failed else {},
        outputs={"candidate": rep.revision.revision_id},
        counts={"changed": rep.changed, "failed": len(rep.failed), "captures": len(rep.revision.entries)},
        quality={"complete": rep.revision.complete, "rule": rep.rule.rule_id,
                 "operation_complete": rep.revision.operation_complete},
        checkpoint={"revision": rep.revision.revision_id, "base": frozen["base"]},
        data={"revision": rep.revision.as_dict(), "rule": rep.rule.as_dict(),
              "activates": bool(req.select and not rep.failed), "failed": rep.failed,
              "diffs": [{"capture_id": d.capture_id, "headline": d.headline(),
                         "changes": [c.as_dict() for c in d.changes],
                         "only_before": d.only_before, "only_after": d.only_after,
                         "ambiguous": d.ambiguous, "boundaries": d.boundaries}
                        for d in rep.diffs]})


@_entry("reparse", write=True)
def reparse(req: ReparseRequest, card=None, *, _operation=None):
    return _reparse(req, card, _operation)


@_entry("compare", write=False)
def compare(req: CompareRequest):
    root = Path(req.dataset)
    _v2(root)
    diff = replay_mod.compare_revisions(root, req.left, req.right)
    return OperationResult(operation="compare",
        counts={"only_left": len(diff.only_left), "only_right": len(diff.only_right),
                "reparsed": len(diff.reparsed)},
        data=dataclasses.asdict(diff))


@_entry("select_revision", write=True)
def select_revision(req: SelectRequest, *, _operation=None):
    root = Path(req.dataset)
    state = _operation
    rev = revisions.read_revision(root, req.revision)
    if "selection" not in state:
        state["selection"] = {"expected": req.expected_active_token or revisions.active_token(root)}
        _save(root, state)
    cur = revisions.active(root)
    if cur.get("operation_id") != state["operation_id"]:
        cur = revisions.activate(root, req.revision, expected=req.expect if req.expect else None,
                                 expected_token=state["selection"]["expected"],
                                 policy=req.policy, allow_incomplete=req.allow_incomplete,
                                 operation_id=state["operation_id"], decision_set_id=req.decision_set_id)
    return OperationResult(operation="select_revision", counts={"entries": len(rev.entries)},
                           outputs={"revision": req.revision},
                           data=cur, checkpoint={"revision": req.revision})


def ledger_view(root, revision_id="", baseline="", view="current", policy=None,
                decision_set_id=None, baseline_decision_set_id=""):
    root = Path(root)
    _v2(root)
    act = revisions.active(root)
    rid = revision_id or act.get("revision_id") or ""
    if not rid:
        raise InputError("还没有选中的版本")
    pol = policy or default_policy(root, rid)
    decisions = decision_set_id if decision_set_id is not None else (
        act.get("decision_set_id", "") if rid == act.get("revision_id") else "")
    led = ledger_mod.build(root, rid, pol, baseline=baseline, decision_set_id=decisions,
                           baseline_decision_set_id=baseline_decision_set_id)
    if view == "current":
        rows, fields = led.current_rows()
    elif view == "history":
        rows, fields = led.history_rows()
    elif view == "pending":
        rows, fields = led.pending_rows()
    elif view == "changes":
        if not baseline:
            raise InputError("看变化要给 baseline")
        rows, fields = led.change_rows()
    else:
        raise InputError(f"没有这个视图 {view!r}")
    return rows, fields, led


@_entry("export", write=True)
def export(req: ExportRequest, policy=None, *, _operation=None):
    root, state = Path(req.dataset), _operation
    if req.format not in ("csv", "md", "xlsx", "jsonl"):
        raise InputError(f"不支持的导出格式：{req.format}", code="unsupported_format")
    if not req.out:
        raise InputError("export 需要 out")
    target = Path(req.out).resolve()
    if "export" not in state:
        rows, fields, led = ledger_view(root, req.revision, req.baseline, req.view, policy,
                                       req.decision_set_id, req.baseline_decision_set_id)
        if not req.include_metadata:
            fields = [f for f in fields if not f.startswith("_")]
            rows = [{f: r.get(f, "") for f in fields} for r in rows]
        if target.exists() and not req.overwrite:
            raise InputError(f"文件已存在：{target}", code="output_exists")
        unparsed = []
        for entry in revisions.read_revision(root, led.revision_id).entries:
            unparsed.extend(revisions.read_parse(root, entry["parse_id"])[1].unparsed)
        state["export"] = {"revision": led.revision_id, "policy": led.policy.as_dict(),
                           "decisions": led.decision_set_id, "baseline": req.baseline,
                           "baseline_decisions": req.baseline_decision_set_id,
                           "view": req.view, "rows": rows, "fields": fields,
                           "unparsed": unparsed, "format": req.format, "target": str(target),
                           "previous_digest": hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None}
        _save(root, state)
    frozen = state["export"]
    target = Path(frozen["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if "artifacts" not in frozen:
        from .render.xlsx import write_any
        from .models import Row
        temporary = target.with_name("." + target.name + "." + state["operation_id"] + ".tmp")
        render = write_any(frozen["format"], [Row(values=r) for r in frozen["rows"]],
                           frozen["fields"], temporary)
        artifacts = [{"kind": "file", "stage": str(temporary), "target": str(target),
                      "previous_digest": frozen["previous_digest"]}]
        if render.companion:
            side = target.with_name(target.stem + ".full.jsonl")
            artifacts.append({"kind": "companion", "stage": str(render.companion),
                              "target": str(side),
                              "previous_digest": hashlib.sha256(side.read_bytes()).hexdigest() if side.exists() else None})
        if frozen["unparsed"]:
            side = target.with_name(target.name + ".unparsed.txt")
            stage = side.with_name("." + side.name + "." + state["operation_id"] + ".tmp")
            stage.write_text("\n".join(frozen["unparsed"]) + "\n", encoding="utf-8")
            artifacts.append({"kind": "unparsed", "stage": str(stage), "target": str(side),
                              "previous_digest": hashlib.sha256(side.read_bytes()).hexdigest() if side.exists() else None})
        for artifact in artifacts:
            artifact["sha256"] = hashlib.sha256(Path(artifact["stage"]).read_bytes()).hexdigest()
        frozen.update(artifacts=artifacts, render_changes=[c.as_dict() for c in render.changes],
                      lossless=render.lossless)
        _save(root, state)  # all expected bytes are known before any target is replaced
    outputs = {}
    for artifact in frozen["artifacts"]:
        dest, stage = Path(artifact["target"]), Path(artifact["stage"])
        actual = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.exists() else None
        if actual != artifact["sha256"]:
            if actual != artifact["previous_digest"]:
                raise InputError(f"目标文件被其他操作修改：{dest}", code="output_conflict")
            if not stage.is_file():
                raise InputError(f"导出的暂存产物缺失：{stage}", code="artifact_missing")
            os.replace(stage, dest)
        else:
            stage.unlink(missing_ok=True)
        outputs[artifact["kind"]] = str(dest)
        outputs["sha256" if artifact["kind"] == "file" else artifact["kind"] + "_sha256"] = artifact["sha256"]
    return OperationResult(operation="export", outputs=outputs,
                           counts={"rows": len(frozen["rows"]), "fields": len(frozen["fields"])},
                           data={"lossless": frozen["lossless"], "render_changes": frozen["render_changes"]},
                           checkpoint={"revision": frozen["revision"], "view": frozen["view"],
                                       "policy_id": frozen["policy"]["policy_id"],
                                       "decision_set_id": frozen["decisions"], "baseline": frozen["baseline"],
                                       "baseline_decision_set_id": frozen["baseline_decisions"]})


@_entry("resolve", write=True)
def resolve(req: Request, *, pending_id: str, revision: str = "",
            policy=None, _operation=None):
    """Create an action for one pending entity; submit its answer through resume."""
    root, state = Path(req.dataset), _operation
    _, _, led = ledger_view(root, revision, view="pending", policy=policy)
    item = next((p for p in led.pending if p.pending_id == pending_id), None)
    if item is None:
        raise InputError("没有这个待处理项", code="not_found")
    token = revisions.active_token(root)
    state["resolution"] = {"pending_id": pending_id, "revision": led.revision_id,
                           "policy": led.policy.as_dict(), "decisions": led.decision_set_id,
                           "expected": token, "candidates": item.candidates}
    action = {"action_id": "action-" + _digest([state["operation_id"], pending_id])[:20],
              "reason": "conflict_resolution",
              "response_via": "resume", "what": "选择候选或保持待处理",
              "pending_id": pending_id, "candidates": item.candidates,
              "input": {"candidate": "candidate_id or keep_pending"}}
    return OperationResult(operation="resolve", status=NEEDS_ACTION,
                           reason="conflict_resolution", action=action,
                           checkpoint={"revision": led.revision_id}, data=item.as_dict())


def _resolve_answer(root, req, state):
    frozen = state["resolution"]
    choice = req.action_response.get("candidate")
    if choice == "keep_pending":
        return OperationResult(operation="resolve", checkpoint=state["checkpoint"],
                               data={"kept_pending": True})
    if choice not in {c["candidate_id"] for c in frozen["candidates"]}:
        raise InputError("回答不是这个待处理项的候选", code="invalid_action")
    current_decisions = revisions._load(root / "decisions" / f"{frozen['decisions']}.json", {}) if frozen["decisions"] else {}
    current_decisions[frozen["pending_id"]] = {
        "candidate": choice, "operation_id": state["operation_id"],
        "reason": req.action_response.get("reason", ""), "at": revisions._now()}
    dsid = "dec-" + _digest(current_decisions)[:20]
    revisions._dump(current_decisions, root / "decisions" / f"{dsid}.json")
    if frozen["revision"] == frozen["expected"]["revision_id"]:
        if revisions.active(root).get("operation_id") != state["operation_id"]:
            revisions.activate(root, frozen["revision"], expected_token=frozen["expected"],
                               policy=frozen["policy"], decision_set_id=dsid,
                               operation_id=state["operation_id"])
    return OperationResult(operation="resolve", outputs={"decision_set_id": dsid,
                           "revision": frozen["revision"]},
                           checkpoint={"revision": frozen["revision"], "decision_set_id": dsid})


@_entry("operation_status", write=False)
def operation_status(req: ResumeRequest):
    root = Path(req.dataset)
    _v2(root)
    oid = req.operation_id or revisions.read_run(root, req.run_id).operation_id
    state = revisions._load(_state_path(root, oid), None)
    if not state:
        raise InputError("没有这个操作", code="not_found")
    return OperationResult(operation="operation_status", operation_id=oid,
                           run_id=state.get("run_id", ""), status=state.get("status", FAILED),
                           reason=next((r["result"]["reason"] for r in reversed(list(state["results"].values()))
                                        if r["result"]["status"] == state.get("status")), ""),
                           checkpoint=state.get("checkpoint", {}), action=state.get("action", {}),
                           counts=state.get("counts", {}), outputs=state.get("outputs", {}),
                           error={"code": "operation_interrupted"} if state.get("status", FAILED) == FAILED else {})


__all__ = ["API_VERSION", "STATUSES", "NEEDS_ACTION", "ACTION_REASONS", "OperationResult",
           "Request", "PreviewRequest", "ExecuteRequest", "ResumeRequest", "ReparseRequest",
           "CompareRequest", "SelectRequest", "ExportRequest", "preview", "execute",
           "resume", "reparse", "compare", "select_revision", "ledger_view", "export", "resolve", "operation_status"]
