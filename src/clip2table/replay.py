"""Reparse stored captures and compare results without fetching source pages.

Candidates retain the baseline capture set, source coordinates and failed-capture
records. Selection remains an explicit operation. Parsing differences and business
changes use separate comparisons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .core import parse_capture
from .errors import InputError
from .models import ParseResult, Row
from . import revisions


# --------------------------------------------------------------- cell diffs


@dataclass
class CellChange:
    """One field of one row, before and after. Both sides carry a line number."""

    key: str
    field_name: str
    before: str
    after: str
    line_before: int | None = None
    line_after: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "field": self.field_name,
                "before": self.before, "after": self.after,
                "line_before": self.line_before, "line_after": self.line_after}

    def line(self) -> str:
        where = ""
        if self.line_after is not None:
            where = f"  ← 第 {self.line_after} 行"
        return f"{self.key} ｜ {self.field_name} ｜ {_show(self.before)} → {_show(self.after)}{where}"


def _show(v: Any, n: int = 40) -> str:
    s = "" if v is None else str(v)
    if not s:
        return "∅"
    s = s.replace("\n", "⏎").replace("\t", "→")
    return s if len(s) <= n else s[:n] + "…"


@dataclass
class ParseDiff:
    """What changed between two parses of one capture."""

    capture_id: str
    before: str
    after: str
    changes: list[CellChange] = field(default_factory=list)
    only_before: list[str] = field(default_factory=list)
    only_after: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    rows_before: int = 0
    rows_after: int = 0
    boundaries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def same(self) -> bool:
        return not (self.changes or self.only_before or self.only_after
                    or self.ambiguous or self.boundaries or self.rows_before != self.rows_after)

    def headline(self) -> str:
        if self.same:
            return f"同一份原文重新解析，结果完全一样（{self.rows_after} 条）"
        bits = []
        if self.changes:
            bits.append(f"{len(self.changes)} 处字段变化")
        if self.only_before or self.only_after:
            bits.append(f"记录 {self.rows_before} → {self.rows_after} 条"
                        f"（{len(self.only_before)} 条只在旧结果里，"
                        f"{len(self.only_after)} 条只在新结果里）")
        if self.ambiguous:
            bits.append(f"{len(self.ambiguous)} 组对不上")
        if self.boundaries:
            bits.append(f"{len(self.boundaries)} 处边界变化")
        return "，".join(bits)

    def unmatched_note(self) -> str:
        """What a reader should conclude when rows exist on only one side.

        A rule that cuts records one line too early splits one order into two;
        a rule that cuts one line too late merges two into one. Both show up as
        "rows that only exist on one side", and neither can be told apart from
        "records that genuinely appeared/disappeared" without the keys. So the
        text names the possibilities instead of picking one.
        """
        if not (self.only_before or self.only_after):
            return ""
        return ("对不上的记录没有按行号强行配对：可能是切分边界变了把一个订单拆成两条"
                "（或把两条并成一条），也可能是内容真的变了。核对主键后再判断。")


def _key_of(row: Row, keys: list[str] | None, index: int) -> tuple[str, bool]:
    """A stable name for a row, and whether it is trustworthy.

    Business keys first: two parses that disagree about how many lines a record
    takes will still agree that order A1 is order A1. Falling back to the source
    line range is only right when the rule that produced both is the same, which
    is exactly what a diff is trying to establish -- so a row whose key cannot be
    formed is reported as ambiguous instead of being matched by position. Position
    matching is how a shift of one line turns into "every row changed".
    """
    if keys:
        vals = [str(row.get(k) or "") for k in keys]
        if all(vals) and len(set(vals)) > 0:
            import json
            return vals[0] if len(vals) == 1 else json.dumps(vals, ensure_ascii=False), True
    if row.raw_at:
        import json
        return "@span:" + json.dumps(row.raw_at), True
    if row.source_line is not None:
        return f"@line:{row.source_line + 1}", False
    return f"@index:{index}", False


def _index_by_key(res: ParseResult, keys: list[str] | None):
    out: dict[str, Row] = {}
    vague: list[str] = []
    for i, row in enumerate(res.rows):
        k, solid = _key_of(row, keys, i)
        if k in out:
            # Two rows claiming the same key is not a diff, it is a duplicate the
            # rule allowed. Keeping the first and saying so beats pretending one
            # of them is the other's "before".
            vague.append(k)
            continue
        out[k] = row
        if not solid:
            vague.append(k)
    return out, vague


def compare_parses(root: str | Path, before_id: str, after_id: str, *,
                   keys: list[str] | None = None) -> ParseDiff:
    """Diff two stored parses. Either can come from a different rule."""
    ref_b, res_b = revisions.read_parse(root, before_id)
    ref_a, res_a = revisions.read_parse(root, after_id)
    if ref_b.capture_id != ref_a.capture_id:
        raise InputError("解析差异只能比较同一捕获的结果", code="different_capture")
    return diff_results(res_b, res_a, capture_id=ref_a.capture_id,
                        before=before_id, after=after_id, keys=keys)


def diff_results(before: ParseResult, after: ParseResult, *,
                 capture_id: str = "", before_id: str = "",
                 after_id: str = "", keys: list[str] | None = None) -> ParseDiff:
    """The actual comparison, usable on results that were never written down."""
    out = ParseDiff(capture_id=capture_id, before=before_id, after=after_id,
                    rows_before=len(before.rows), rows_after=len(after.rows))
    idx_b, vague_b = _index_by_key(before, keys)
    idx_a, vague_a = _index_by_key(after, keys)
    for k in sorted(set(idx_b) & set(idx_a)):
        if k in vague_b or k in vague_a:
            continue
        rb, ra = idx_b[k], idx_a[k]
        if rb.raw_at != ra.raw_at:
            out.boundaries.append({"key": k, "before": rb.raw_at, "after": ra.raw_at})
        for name in list(dict.fromkeys(after.fields + before.fields)):
            val_b = rb.get(name) if hasattr(rb, "get") else None
            val_a = ra.get(name) if hasattr(ra, "get") else None
            if val_b == val_a:
                continue
            tb = rb.traces.get(name) if getattr(rb, "traces", None) else None
            ta = ra.traces.get(name) if getattr(ra, "traces", None) else None
            out.changes.append(CellChange(
                key=k, field_name=name,
                before="" if val_b is None else str(val_b),
                after="" if val_a is None else str(val_a),
                line_before=_abs_line(rb, tb), line_after=_abs_line(ra, ta)))
    out.only_before = sorted(set(idx_b) - set(idx_a))
    out.only_after = sorted(set(idx_a) - set(idx_b))
    # An "ambiguous" label is only worth printing when it explains a row that
    # could not be matched. Listing every row that merely lacked a business key
    # would make a clean diff look full of doubt.
    unmatched = set(out.only_before) | set(out.only_after)
    out.ambiguous = sorted(set(vague_b + vague_a))
    for kb in out.only_before:
        rb = idx_b[kb]
        matches = [ka for ka in out.only_after
                   if set(rb.raw_at) & set(idx_a[ka].raw_at)]
        if matches:
            out.boundaries.append({"before_key": kb, "after_keys": matches,
                                   "kind": "split_or_merge", "basis": "source_overlap"})
    return out


def _abs_line(row: Row, trace) -> int | None:
    if trace is not None and getattr(trace, "line", None) is not None:
        if row.raw_at and 0 <= trace.line < len(row.raw_at):
            return row.raw_at[trace.line] + 1
        if row.source_line is not None:
            return row.source_line + trace.line + 1
    return None


# ----------------------------------------------------------- one replay


@dataclass
class Reparse:
    """A candidate revision plus the per-capture diffs that justify it."""

    revision: revisions.ResultRevision
    rule: revisions.RuleSnapshot
    diffs: list[ParseDiff] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(len(d.changes) for d in self.diffs)

    def headline(self) -> str:
        bits = [f"候选版本 {self.revision.revision_id}，"
                f"{len(self.revision.entries)} 份原文重新解析"]
        if self.changed:
            bits.append(f"{self.changed} 处字段变化")
        else:
            bits.append("字段没有变化")
        if self.failed:
            bits.append(f"{len(self.failed)} 份失败")
        return "；".join(bits)


def rule_for(root: str | Path, *, card=None, rule_id: str = "",
             base: revisions.ResultRevision | None = None) -> revisions.RuleSnapshot:
    """Pick the rule a replay runs under, and say how it was picked.

    An explicit card wins: the whole point is to try an edit before saving it.
    Otherwise an explicit rule id, then the rule that produced the base
    revision's parses -- replaying with "whatever rule is current" would make the
    same command mean something different tomorrow.
    """
    if card is not None:
        return revisions.write_rule(root, card)
    if rule_id:
        return revisions.load_rule(root, rule_id)
    if base is not None:
        for pid in base.parses:
            pref = revisions.list_parses(root)
            for p in pref:
                if p.parse_id == pid and p.rule_id:
                    return revisions.load_rule(root, p.rule_id)
    raise InputError("不知道该用哪一套规则重解析",
                     hint="给 --card 试一份改过的卡，或者 --rule 指定已有的规则快照")


@revisions.writing
def reparse(root: str | Path, base_revision: str, *, card=None,
            rule_id: str = "", captures: Iterable[str] | None = None,
            reason: str = "", compare: bool = True) -> Reparse:
    """Parse saved captures again under a rule, publishing a candidate.

    The candidate is never activated here. Callers decide that, and they have to
    name it -- `revisions.activate(..., expected=base)` refuses if something else
    was selected in the meantime.
    """
    root = Path(root)
    base = revisions.read_revision(root, base_revision)
    picked = list(captures) if captures else base.captures
    if not picked:
        raise InputError(f"版本 {base_revision} 里没有捕获，没有可重解析的内容")
    unknown = [c for c in picked if c not in base.captures]
    if unknown:
        raise InputError(
            f"这些捕获不在版本 {base_revision} 里：{unknown}",
            hint="重解析的范围必须是某个版本已经包含的捕获，"
                 "否则候选版本会悄悄变成一份更大的台账")

    rule = rule_for(root, card=card, rule_id=rule_id, base=base)
    try:
        new_card = revisions.card_from_rule(rule)
    except InputError as e:
        raise InputError(f"规则快照还原不出可用的卡：{e.message}")

    out = Reparse(revision=base, rule=rule)
    entries: list[tuple[str, str]] = []
    for cid in picked:
        try:
            cap_ref, cap = revisions.read_capture(root, cid)
            res, _ = parse_capture(cap, new_card)
        except (InputError, OSError, ValueError) as e:
            out.failed.append({"capture_id": cid, "why": f"{type(e).__name__}: {e}"})
            # Keep the base parse in the candidate: dropping it would make the
            # candidate silently cover less than the revision it descends from,
            # and A12 is precisely about not treating "not re-parsed" as "gone".
            old = _parse_of(base, cid)
            if old:
                entries.append((cid, old))
            continue
        pref = revisions.write_parse(root, cap_ref, rule, res)
        entries.append((cid, pref.parse_id))
        if compare:
            old_id = _parse_of(base, cid)
            if old_id:
                _, old_res = revisions.read_parse(root, old_id)
                out.diffs.append(diff_results(
                    old_res, res, capture_id=cid, before_id=old_id,
                    after_id=pref.parse_id,
                    keys=list(new_card.dedupe_key or [])))

    note = ""
    if out.failed:
        note = f"{len(out.failed)} 份原文没能重新解析，沿用原结果"
    # Reparsing a subset must not publish a ledger missing the pages it did not
    # look at. `carry_forward` supplies the rest from the base revision, keyed by
    # page so a canonical page cannot appear twice.
    entries = revisions.carry_forward(root, base_revision, entries)
    out.revision = revisions.write_revision(
        root, entries, parent=base_revision, reason=reason or "reparse", note=note,
        failed=out.failed, requested_captures=picked, target_rule=rule.rule_id,
        operation_complete=not out.failed)
    return out


def _parse_of(rev: revisions.ResultRevision, capture_id: str) -> str:
    for e in rev.entries:
        if e.get("capture_id") == capture_id:
            return e.get("parse_id") or ""
    return ""


# ---------------------------------------------------- revision-level view


@dataclass
class RevisionDiff:
    """Which captures each side covers, and which parses it points at."""

    left: str
    right: str
    only_left: list[str] = field(default_factory=list)
    only_right: list[str] = field(default_factory=list)
    reparsed: list[str] = field(default_factory=list)

    def headline(self) -> str:
        bits = [f"{self.left} → {self.right}"]
        if self.only_left:
            bits.append(f"{len(self.only_left)} 份原文只在左边")
        if self.only_right:
            bits.append(f"{len(self.only_right)} 份原文只在右边")
        if self.reparsed:
            bits.append(f"{len(self.reparsed)} 份换了规则重解析")
        if len(bits) == 1:
            bits.append("两边覆盖的原文完全相同")
        return "；".join(bits)


def compare_revisions(root: str | Path, left: str, right: str) -> RevisionDiff:
    a = revisions.read_revision(root, left)
    b = revisions.read_revision(root, right)
    out = RevisionDiff(left=left, right=right)
    out.only_left = [c for c in a.captures if c not in b.captures]
    out.only_right = [c for c in b.captures if c not in a.captures]
    for cid in b.captures:
        pa, pb = _parse_of(a, cid), _parse_of(b, cid)
        if pa and pb and pa != pb:
            out.reparsed.append(cid)
    return out
