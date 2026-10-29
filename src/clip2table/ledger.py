"""Derive current, history, changes and pending views from saved observations.

Typed business keys group observations. Explicit business ordering selects current
values; conflicts retain the accepted value and candidates for a recorded decision.
Every view pins a revision, policy and decision set.
"""
from __future__ import annotations

import dataclasses
import json
import hashlib
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError
from .models import Row
from . import revisions

# How a task says "which observation is the newest".
#
# `observed` is the fallback and it is honest: "the latest thing I saw" is a real
# answer as long as it is *declared* to be the rule. What is not allowed is using
# it silently, because then a late-arriving old page rewrites the ledger, and
# every later comparison is against a table that moved backwards.
SORTS = ("business_time", "business_version", "observed")
TYPED = ("text", "int", "float", "date")


def policy_from_card(card) -> "LedgerPolicy":
    return LedgerPolicy(keys=list(card.dedupe_key), identity=list(card.identity_fields),
                        mutable=list(card.mutable_fields), sort=card.ledger_sort,
                        sort_field=card.ledger_sort_field,
                        timezone=str(card.ledger.get("timezone", "Asia/Shanghai")),
                        key_types=dict(card.ledger.get("key_types") or {}))


@dataclass
class LedgerPolicy:
    """How to read one task's rows as a ledger. Stored next to the revision.

    `keys` is the business identity. `identity` fields may never change for an
    entity (changing one is a different entity or an error, both of which are
    reported rather than guessed). `mutable` fields are the ones allowed to change;
    any *other* field changing is reported as pending, because a rule that begins
    returning a different value for a column nobody declared as changing is a
    configuration problem, not a business event.
    """

    keys: list[str] = field(default_factory=list)
    identity: list[str] = field(default_factory=list)
    mutable: list[str] = field(default_factory=list)
    sort: str = "observed"
    sort_field: str = ""
    timezone: str = "Asia/Shanghai"
    key_types: dict[str, str] = field(default_factory=dict)
    policy_id: str = ""

    def __post_init__(self) -> None:
        if self.sort not in SORTS:
            raise InputError(f"排序方式只能是 {SORTS} 之一，不是 {self.sort!r}")
        if self.sort != "observed" and not self.sort_field:
            raise InputError(
                f"排序方式选了 {self.sort}，但没说是哪个字段",
                hint="业务时间给 --sort-field 一个时间字段；业务版本给一个版本号字段")
        overlap = set(self.identity) & set(self.mutable)
        if overlap:
            raise InputError(f"这些字段同时被声明为身份字段和可变字段：{sorted(overlap)}")
        bad = [t for t in self.key_types.values() if t not in TYPED]
        if bad:
            raise InputError(f"主键类型只能是 {TYPED}，不是 {bad}")
        if not self.policy_id:
            import hashlib
            import json
            body = json.dumps({
                "keys": self.keys, "identity": self.identity,
                "mutable": self.mutable, "sort": self.sort,
                "sort_field": self.sort_field, "timezone": self.timezone,
                "key_types": self.key_types,
            }, sort_keys=True, ensure_ascii=False)
            self.policy_id = "pol-" + hashlib.sha256(
                body.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LedgerPolicy":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def key_of(self, row: Row) -> tuple[str, str]:
        """(typed identity, a readable label). Raises when a key is missing.

        A composite key is joined with a separator that cannot appear in a value,
        and each part is typed, so "0123" and "123" stay two orders. A key with a
        missing part is not "the other parts"; `None` is returned instead of an
        empty string so a caller cannot accidentally merge them.
        """
        parts: list[str] = []
        for name in self.keys:
            raw = row.get(name)
            if raw is None or str(raw).strip() == "":
                raise KeyError(name)
            kind = self.key_types.get(name, "text")
            parts.append(_typed(str(raw).strip(), kind))
        return (parts[0] if len(parts) == 1 else json.dumps(list(zip(self.keys, parts)), ensure_ascii=False)), " · ".join(
            str(row.get(n)).strip() for n in self.keys)


def _typed(value: str, kind: str) -> str:
    """Normalise a key part by its declared type.

    Text is kept as written -- a leading zero is part of the order number, and
    stripping it is how 0123 becomes 123 and two different orders become one.
    Numbers are normalised so "1" and "1.0" are the same entity.
    """
    if kind == "int":
        try:
            number = Decimal(value)
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError(value)
            return f"int:{int(number)}"
        except InvalidOperation:
            raise ValueError(value) from None
    if kind == "float":
        try:
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError(value)
            return f"float:{number.normalize()}"
        except InvalidOperation:
            raise ValueError(value) from None
    if kind == "date":
        d = _parse_when(value, "UTC")
        if d is None:
            raise ValueError(value)
        return f"date:{d.date().isoformat()}"
    return f"text:{value}"


def _parse_when(value: str, tz: str) -> datetime | None:
    """Parse the date formats a business column actually contains."""
    v = (value or "").strip()
    if not v:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
            "%Y年%m月%d日 %H:%M", "%Y年%m月%d日", "%Y%m%d")
    for f in fmts:
        try:
            return datetime.strptime(v, f).replace(tzinfo=_tz(tz))
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=_tz(tz))
    except ValueError:
        return None


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (ImportError, KeyError):
        from datetime import timedelta
        if name in ("Asia/Shanghai", "Asia/Chongqing"):
            return timezone(timedelta(hours=8))
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        raise InputError(f"无法读取时区 {name!r}", code="bad_timezone") from None


@dataclass
class EntityVersion:
    """One entity as one observation saw it.

    `source` keeps the capture / parse / row triple, so any value in any view can
    be traced back to the page and line it came from. Without it, "history" is a
    table of guesses: you can see that the status changed, not what evidence said
    so.
    """

    key: str
    label: str
    values: dict[str, str] = field(default_factory=dict)
    sort_value: Any = None
    sort_text: str = ""
    capture_id: str = ""
    parse_id: str = ""
    rule_id: str = ""
    page: int | None = None
    observed_at: str = ""
    row: Row | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "values": dict(self.values),
                "sort_value": _jsonable(self.sort_value), "sort_text": self.sort_text,
                "capture_id": self.capture_id, "parse_id": self.parse_id,
                "rule_id": self.rule_id, "page": self.page,
                "observed_at": self.observed_at}


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _sort_value(row: Row, policy: LedgerPolicy, observed_at: str):
    """The value that decides which observation is the newer one.

    Returns (comparable, printable). `observed` compares the observation time,
    which is always available and always honest -- it just is not a business
    fact, so it is only used when the task says so.
    """
    if policy.sort == "observed":
        when = _parse_when(observed_at, policy.timezone)
        return (when.timestamp() if when else None), observed_at
    raw = str(row.get(policy.sort_field) or "").strip()
    if policy.sort == "business_version":
        parts = raw.split(".")
        nums = [int(x) for x in parts] if all(x.isdigit() for x in parts) else []
        val = tuple(nums) if nums else ()
        return val or None, raw
    when = _parse_when(raw, policy.timezone)
    return (when.timestamp() if when else None), raw


# ------------------------------------------------------------ version set


@dataclass
class VersionSet:
    """Every observation of every entity, plus what each one was based on."""

    versions: list[EntityVersion] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    scope: list[dict[str, Any]] = field(default_factory=list)
    policy: LedgerPolicy = field(default_factory=LedgerPolicy)

    def by_key(self) -> dict[str, list[EntityVersion]]:
        out: dict[str, list[EntityVersion]] = {}
        for v in self.versions:
            out.setdefault(v.key, []).append(v)
        return out


def collect_versions(root: str | Path, revision_id: str,
                     policy: LedgerPolicy) -> VersionSet:
    """Read one revision's parses and produce entity versions.

    Each parse is read with its own `rule_id`: a version carries the rule that
    produced it, which is the only way a later comparison can tell a rule effect
    from a business change.
    """
    root = Path(root)
    rev = revisions.read_revision(root, revision_id)
    out = VersionSet(policy=policy)
    parses = {p.parse_id: p for p in revisions.list_parses(root)}
    for entry in rev.entries:
        cid, pid = entry.get("capture_id", ""), entry.get("parse_id", "")
        if not pid:
            out.skipped.append({"capture_id": cid, "why": "这个捕获没有解析结果"})
            continue
        pref = parses.get(pid)
        if pref is None:
            out.skipped.append({"capture_id": cid, "why": f"缺解析结果 {pid}"})
            continue
        try:
            meta = revisions._load(root / revisions.CAPTURES / cid / "meta.json", None)
            if meta is None:
                raise InputError("捕获元数据缺失")
            cref = revisions.CaptureRef.from_dict(meta)
        except InputError as e:
            out.skipped.append({"capture_id": cid, "why": e.message})
            continue
        _, res = revisions.read_parse(root, pid)
        out.scope.append({"capture_id": cid, "page": cref.page,
                          "kind": cref.kind, "observed_at": cref.observed_at,
                          "rows": len(res.rows), "rule_id": pref.rule_id})
        for row in res.rows:
            try:
                key, label = policy.key_of(row)
            except (KeyError, ValueError) as e:
                # A row without its business key cannot be sequenced against
                # other observations. Reported by name, never merged into a
                # neighbour: two orders sharing a key is the classic "少了一个
                # 订单" bug and it starts exactly here.
                out.skipped.append({"capture_id": cid,
                                    "why": f"第 {row.source_line} 行主键缺失或类型不符：{e.args[0]}",
                                    "parse_id": pid, "row": row.source_line,
                                    "values": dict(row.values)})
                continue
            value, text = _sort_value(row, policy, cref.observed_at)
            out.versions.append(EntityVersion(
                key=key, label=label,
                values={k: ("" if row.get(k) is None else str(row.get(k)))
                        for k in (res.fields or [])},
                sort_value=value, sort_text=text,
                capture_id=cid, parse_id=pid, rule_id=pref.rule_id,
                page=cref.page, observed_at=cref.observed_at, row=row))
    return out


# -------------------------------------------------------------- the views


@dataclass
class CurrentEntity:
    label: str
    values: dict[str, str]
    chosen: EntityVersion
    basis: str
    pending: list[str] = field(default_factory=list)
    alternatives: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "values": dict(self.values),
                "basis": self.basis, "pending": list(self.pending),
                "alternatives": self.alternatives,
                "source": self.chosen.as_dict()}


@dataclass
class FieldChange:
    field_name: str
    before: str
    after: str

    def line(self) -> str:
        return f"{self.field_name}：{_show(self.before)} → {_show(self.after)}"


@dataclass
class ChangedEntity:
    label: str
    kind: str            # new | updated | rule_reparse | pending
    changes: list[FieldChange] = field(default_factory=list)
    why: str = ""
    before: EntityVersion | None = None
    after: EntityVersion | None = None
    pending_id: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "why": self.why,
                "changes": [dataclasses.asdict(c) for c in self.changes],
                "before": self.before.as_dict() if self.before else None,
                "after": self.after.as_dict() if self.after else None,
                "pending_id": self.pending_id, "candidates": self.candidates}


def _show(v: str, n: int = 30) -> str:
    s = "" if v is None else str(v)
    if not s:
        return "∅"
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[:n] + "…"


@dataclass
class Ledger:
    """The three views, all derived from the same version set."""

    policy: LedgerPolicy
    revision_id: str
    baseline_id: str = ""
    current: list[CurrentEntity] = field(default_factory=list)
    history: dict[str, list[EntityVersion]] = field(default_factory=dict)
    changes: list[ChangedEntity] = field(default_factory=list)
    pending: list[ChangedEntity] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    scope: list[dict[str, Any]] = field(default_factory=list)
    decision_set_id: str = ""

    # -- serialisation used by both the CLI and the API -------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.as_dict(),
            "revision_id": self.revision_id,
            "baseline_id": self.baseline_id,
            "current": [c.as_dict() for c in self.current],
            "changes": [c.as_dict() for c in self.changes],
            "pending": [c.as_dict() for c in self.pending],
            "history": {k: [v.as_dict() for v in vs]
                        for k, vs in self.history.items()},
            "notes": list(self.notes),
            "skipped": list(self.skipped),
            "scope": list(self.scope),
            "decision_set_id": self.decision_set_id,
        }

    # -- flat export -----------------------------------------------------
    def current_rows(self) -> tuple[list[dict[str, str]], list[str]]:
        fields: list[str] = []
        for c in self.current:
            for f in c.values:
                if f not in fields:
                    fields.append(f)
        rows = [dict(c.values, **{"_pending": "；".join(c.pending)}) for c in self.current]
        return rows, fields + ["_pending"]

    def change_rows(self) -> tuple[list[dict[str, str]], list[str]]:
        """One row per changed field; a whole new entity gets one row, filled in.

        Without the `after` summary a new entity reads as `new ｜ A3 ｜  ｜  ｜  `
        -- true, and useless: the reader cannot see what was added and has to go
        look it up in another view.

        A pending entity can be named twice -- once because two observations in
        this revision disagree on an undeclared field, once because the baseline
        disagrees with the chosen version. Both are the same fact told from two
        sides, so the baseline row wins (it has before/after) and the duplicate
        is dropped.
        """
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for group in (self.changes, self.pending):
            for c in group:
                entries = ([{"字段": ch.field_name, "之前": ch.before, "之后": ch.after}
                            for ch in c.changes]
                           if c.changes else
                           [{"字段": "（整条）", "之前": "",
                             "之后": "；".join(f"{k}={_show(v, 20)}"
                                              for k, v in (c.after.values.items()
                                                           if c.after else []))}])
                for e in entries:
                    mark = (c.label, e["字段"])
                    if mark in seen:
                        continue
                    seen.add(mark)
                    out.append({"类型": c.kind, "实体": c.label, **e, "说明": c.why})
        return out, ["类型", "实体", "字段", "之前", "之后", "说明"]

    def pending_rows(self) -> tuple[list[dict[str, str]], list[str]]:
        out = [{"实体": c.label, "原因": c.why,
                "字段": "；".join(ch.field_name for ch in c.changes)}
               for c in self.pending]
        return out, ["实体", "原因", "字段"]

    def history_rows(self) -> tuple[list[dict[str, str]], list[str]]:
        """Every observation, newest first. The A->B->A trail lives here.

        Under `observed` the sort value *is* the observation time, so printing
        both would read as two independent facts that agree on every row.
        """
        by_observation = self.policy.sort == "observed"
        rows: list[dict[str, str]] = []
        for _key, versions in self.history.items():
            for v in versions:
                rows.append({
                    "_实体": v.label,
                    "_排序值": "（按观察时间）" if by_observation else (v.sort_text or "—"),
                    "_页面": str(v.page),
                    "_观察时间": v.observed_at,
                    **{k: str(val) for k, val in v.values.items()},
                })
        fields = ["_实体", "_排序值", "_页面", "_观察时间"]
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
        return rows, fields


def build(root: str | Path, revision_id: str, policy: LedgerPolicy, *,
          baseline: str = "", decision_set_id: str = "",
          baseline_decision_set_id: str = "") -> Ledger:
    """Assemble the three views for one revision against one baseline.

    `baseline` is explicit on purpose. "Changed since last time" needs a "last
    time", and reading it from `active.json` would mean two people comparing
    against different tables while both saying "since last time".
    """
    root = Path(root)
    vs = collect_versions(root, revision_id, policy)
    led = Ledger(policy=policy, revision_id=revision_id, baseline_id=baseline,
                 skipped=vs.skipped, scope=vs.scope, decision_set_id=decision_set_id)
    decisions = revisions._load(root / "decisions" / f"{decision_set_id}.json", {}) if decision_set_id else {}
    groups = vs.by_key()

    for key in sorted(groups, key=lambda k: groups[k][0].label):
        ordered = _sequence(groups[key])
        led.history[key] = ordered
        pick, pending = _choose(ordered, policy)
        signature = json.dumps([policy.policy_id, key,
                                sorted({json.dumps([v.rule_id, v.values, _jsonable(v.sort_value)],
                                                   ensure_ascii=False, sort_keys=True) for v in ordered})],
                               ensure_ascii=False, sort_keys=True)
        pending_id = "pend-" + hashlib.sha256(signature.encode()).hexdigest()[:20]
        decision = decisions.get(pending_id, {})
        selected = next((v for v in ordered if decision.get("candidate") == candidate_id(v)), None)
        if selected:
            pick, pending = selected, []
        if pick is not None:
            led.current.append(CurrentEntity(
            label=ordered[0].label, values=dict(pick.values),
            chosen=pick, basis=pick.sort_text or "未声明排序值",
            pending=pending, alternatives=max(0, len(ordered) - 1)))
        if pending:
            # The alternative is the next best version, not `ordered[0]` -- that
            # is the one we just picked, so "before" would have been a copy of
            # "after" and the pending row would show no difference at all.
            alt = pick
            chosen = next((v for v in ordered if pick is None or v.values != pick.values), ordered[0])
            moved = [FieldChange(n, alt.values.get(n, "") if alt else "",
                                 chosen.values.get(n, ""))
                     for n in chosen.values
                     if alt is not None and alt.values.get(n, "") != chosen.values.get(n, "")]
            led.pending.append(ChangedEntity(
                label=ordered[0].label, kind="pending",
                why="；".join(pending), after=chosen, before=alt, changes=moved,
                pending_id=pending_id,
                candidates=[dict(v.as_dict(), candidate_id=candidate_id(v)) for v in ordered]))

    for item in vs.skipped:
        pid = "pend-" + hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        led.pending.append(ChangedEntity(label=f"{item.get('capture_id')}:{item.get('row')}",
                                         kind="pending", why=item["why"], pending_id=pid))

    if baseline:
        led.changes = _changes_against(root, baseline, led, policy, baseline_decision_set_id)
    return led


def _sequence(versions: list[EntityVersion]) -> list[EntityVersion]:
    """Newest first, and a stable tie-break.

    Two observations with the same business sort value are exactly the ambiguous
    case the contracts call out. They are ordered by observation time so the list
    is deterministic, and `_choose` reports the tie instead of letting this sort
    order pretend to be a business decision.
    """
    def key(v: EntityVersion):
        sv = v.sort_value
        if isinstance(sv, tuple):
            primary = sv
        else:
            primary = (sv if sv is not None else float("-inf"),)
        # Padding has to happen inside a single comparable tuple, so mixed tuple
        # lengths (business version "1.2" vs "1.10") must be normalised first.
        width = 4
        primary = tuple(primary) + (float("-inf"),) * max(0, width - len(primary))
        when = _parse_when(v.observed_at, "UTC")
        return (primary, when.timestamp() if when else 0.0)
    return sorted(versions, key=key, reverse=True)


def candidate_id(v: EntityVersion) -> str:
    body = json.dumps([v.rule_id, v.values, _jsonable(v.sort_value)],
                      ensure_ascii=False, sort_keys=True)
    return "choice-" + hashlib.sha256(body.encode()).hexdigest()[:20]


def _choose(ordered: list[EntityVersion],
            policy: LedgerPolicy) -> tuple[EntityVersion | None, list[str]]:
    """Pick the current version, and name anything a human must decide.

    Three situations produce pending rather than a guess: a tie on the business
    sort value with different content, an identity field that changed, and a
    field that changed that the task never declared mutable. In each case the
    candidate is kept (so nothing is lost) and the reason is printed.
    """
    if not ordered:
        raise InputError("空版本集")
    pending: list[str] = []
    pick = None
    for v in sorted(ordered, key=lambda v: v.observed_at):
        if v.sort_value is None:
            pending.append("业务排序值缺失或无效")
            continue
        if pick is None:
            pick = v
            continue
        changed = {n for n in set(v.values) | set(pick.values)
                   if v.values.get(n, "") != pick.values.get(n, "")}
        identity = changed & set(policy.identity)
        undeclared = changed - set(policy.keys) - set(policy.mutable) - set(policy.identity)
        if identity:
            pending.append(f"身份字段变化：{sorted(identity)}")
        elif undeclared:
            pending.append(f"这些字段没被声明会变：{sorted(undeclared)}")
        elif changed and v.sort_value == pick.sort_value:
            pending.append(f"排序值相同（{v.sort_text}）但内容不同")
        elif v.sort_value > pick.sort_value:
            pick = v
    return pick, list(dict.fromkeys(pending))


def _changes_against(root: Path, baseline: str, led: Ledger,
                     policy: LedgerPolicy, baseline_decision_set_id: str = "") -> list[ChangedEntity]:
    """Compare two revisions as business change, keeping rule effects separate.

    The rule check comes first: if the two sides were parsed by different rules,
    a differing value says nothing about the business until the rule is the same.
    Marking it `rule_reparse` is not a refusal to compare -- the before and after
    are still shown -- it is a refusal to *call* it a business change.
    """
    bvs = collect_versions(root, baseline, policy)
    bgroups = bvs.by_key()
    # The baseline has to answer the same question the current side does: "which
    # version of this entity was current *then*". Taking the first observation
    # would compare today's value against whatever happened to be read first,
    # and report an identity change that never happened -- with three pages of
    # history on one order, the first two pages look like the customer changed.
    baseline_view = build(root, baseline, policy, decision_set_id=baseline_decision_set_id)
    bpick = {c.chosen.key: c.chosen for c in baseline_view.current}
    out: list[ChangedEntity] = []
    # The current side is the *chosen* version per entity, not every observation:
    # a change table that lists each observation separately would say "A1234 changed"
    # three times for one status that was only read three times.
    cur_groups = {c.chosen.key: c.chosen for c in led.current}
    # An entity whose current version is itself uncertain (a tie on the business
    # sort value, an identity change, an undeclared field) must not appear here as
    # an ordinary `updated` row: "已发货 → 已完成" reads as a business fact, and the
    # whole point of the pending list is that this particular pair is not one.
    unsettled = {c.chosen.key: c.pending for c in led.current if c.pending}
    for key in sorted(set(bpick) | set(cur_groups),
                      key=lambda k: (cur_groups.get(k) or bpick[k]).label):
        before = bpick.get(key)
        after = cur_groups.get(key)
        label = (after or before).label
        if after is None:
            # Not observed in this revision. That is a scope statement, not a
            # deletion: the view reports the observed pages instead of inventing
            # a business event out of absence.
            continue
        if key in unsettled:
            item = next(p for p in led.pending if p.label == label)
            out.append(ChangedEntity(label=label, kind="pending", changes=item.changes,
                                     before=before, after=item.after, why=item.why,
                                     pending_id=item.pending_id, candidates=item.candidates))
            continue
        if before is None:
            out.append(ChangedEntity(label=label, kind="new", after=after,
                                     why="这次观察里才有这个实体"))
            continue
        b = before
        if b.rule_id != after.rule_id:
            diff = [FieldChange(n, b.values.get(n, ""), after.values.get(n, ""))
                    for n in after.values
                    if b.values.get(n, "") != after.values.get(n, "")]
            if diff:
                out.append(ChangedEntity(
                    label=label, kind="rule_reparse", changes=diff,
                    before=b, after=after,
                    why=f"两次观察用的规则不同（{b.rule_id} → {after.rule_id}），"
                        f"值不一样首先说明规则变了，不一定是业务变了"))
            continue
        diff = [FieldChange(n, b.values.get(n, ""), after.values.get(n, ""))
                for n in after.values
                if b.values.get(n, "") != after.values.get(n, "")]
        if not diff:
            continue
        undeclared = [d.field_name for d in diff
                      if d.field_name not in policy.mutable
                      and d.field_name not in policy.keys]
        if undeclared:
            out.append(ChangedEntity(
                label=label, kind="pending", changes=diff, before=b, after=after,
                why=f"这些字段没被声明会变：{undeclared}"))
        elif key in unsettled:
            out.append(ChangedEntity(
                label=label, kind="pending", changes=diff, before=b, after=after,
                why="；".join(unsettled[key])))
        else:
            if policy.sort == "observed":
                why = f"观察时间 {b.observed_at} → {after.observed_at}"
            else:
                why = (f"按 {policy.sort_field}：{b.sort_text or '—'}"
                       f" → {after.sort_text or '—'}")
            out.append(ChangedEntity(label=label, kind="updated", changes=diff,
                                     before=b, after=after, why=why))
    led_notes: list[str] = []
    if bvs.scope and led.scope and \
            {s["capture_id"] for s in bvs.scope} != {s["capture_id"] for s in led.scope}:
        led_notes.append(
            f"两次观察覆盖的原文不一样（基线 {len(bvs.scope)} 份，"
            f"现在 {len(led.scope)} 份）。没被这次观察到的实体按原样保留，"
            f"不当作删除。")
    led.notes.extend(led_notes)
    return out
