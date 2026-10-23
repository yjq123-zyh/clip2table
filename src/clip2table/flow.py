"""flow: the one path from "a page arrived" to "the dataset moved".

Why this module exists
----------------------
Every command that writes used to re-own a piece of the same sequence. `harvest`
decided the dataset, merged the page, saved state and printed inside a callback;
`run` accepted `-d` from the shared argument group and then never used it;
`export` read the dataset back with its own idea of totals. The three copies
disagreed in ways nobody could see from the outside:

* `run -d some/dir` parsed the capture, printed a report and dropped the rows --
  the flag was accepted, the directory was never created, and nothing said so.
* `--dry-run` meant "skip the commit" in one place and "still save state" in
  another, because the guard sat inside the caller rather than around the write.
* "how many rows did this run add" and "how many rows are in the directory"
  were printed by the same code path, so a resumed run mixed the two.

So the write boundary lives here and only here. A command may fetch, parse and
print; it may not touch the dataset except through `Committer`. `--dry-run` is
not a flag the committer checks per write -- it is the absence of a committer,
so a preview cannot half-write.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from .errors import InputError
from .models import ParseResult

# Totals are the same shape everywhere, including the JSON report, so that
# "changed" cannot quietly mean one thing in a print and another in a file.
TOTAL_KEYS = ("added", "duplicates", "conflicts", "empty_keys",
              "changed", "identity_conflicts")


@dataclass
class Totals:
    """What one command did. Fetched and committed are deliberately separate.

    `fetched_rows` is what the pages produced; `added` is what the dataset
    accepted. They differ whenever a page was a duplicate, a page was never
    committed, or the run is a preview -- which is exactly the confusion this
    object exists to prevent.
    """

    fetched_rows: int = 0
    fetched_lines: int = 0
    pages: int = 0
    added: int = 0
    duplicates: int = 0
    conflicts: int = 0
    empty_keys: int = 0
    changed: int = 0
    identity_conflicts: int = 0
    # A page whose text was already published. Separate from `duplicates`,
    # which counts ROWS: one repeated page can be zero new rows, and zero new
    # rows can also come from a page full of records that all already existed.
    repeated: int = 0
    repeat_of: int | None = None

    def fetched(self, res: ParseResult) -> None:
        self.fetched_rows += len(res.rows)
        self.fetched_lines += res.consumed_lines

    def record_outcome(self, outcome) -> None:
        self.added += outcome.added
        self.duplicates += outcome.duplicates
        self.conflicts += outcome.conflicts
        self.empty_keys += outcome.empty_keys
        self.changed += outcome.changed
        self.identity_conflicts += outcome.identity_conflicts

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in TOTAL_KEYS}


class Committer:
    """The only writer. No dataset, no writes; dry run, it simply is not built.

    A caller that wants a preview gets `None` from `open_dataset` and therefore
    has no object to write through, which is stronger than a boolean every call
    site has to remember to check.
    """

    def __init__(self, ds, card_name: str):
        self.ds = ds
        self.card_name = card_name
        self.totals = Totals()

    def page(self, page: int, raw: str, meta: dict[str, Any],
             res: ParseResult, append_from_here: bool = False):
        """Publish one page, then advance the resume position.

        Order is enforced by `Dataset.commit_page`: nothing outside `pages/`
        moves until the page is whole on disk, and the position moves last. A
        page that was fetched but not committed must not move the position, so
        this is the only place it advances.
        """
        self.totals.pages += 1
        commit = self.ds.commit_page(page, raw, meta, res,
                                     append_from_here=append_from_here)
        if commit.duplicate_of is not None:
            # The same screen again. Counting it would double the rows for one
            # page of content; advancing the position would claim progress that
            # did not happen. Say it out loud instead.
            self.totals.repeated += 1
            self.totals.repeat_of = commit.duplicate_of
            print(f"  第 {page} 页和第 {commit.duplicate_of} 页的原文完全相同，"
                  f"这一页没有发布，续采位置不动", file=sys.stderr)
            return commit
        self.ds.state["card"] = self.card_name
        self.ds.save()
        if commit.published:
            self.totals.record_outcome(commit.outcome)
            self._report_collisions(page, commit.outcome)
        return commit

    def _report_collisions(self, page: int, outcome) -> None:
        if not outcome.conflicts:
            return
        kept_first = self.ds.on_collision != "keep_all"
        for c in outcome.details[:5]:
            which = ("内容变更" if c.verdict == "change"
                     else "身份冲突（身份字段变了，可能不是同一条记录）")
            fields = "、".join(c.changed_fields[:4]) or "（未知）"
            tail = ("→ 保留先前那条，新的丢弃" if kept_first
                    else "→ 两条都留下，新的标了 _dup_of")
            hint = f"（{c.hint}）" if c.hint else ""
            print(f"  {which}（第 {page} 页）{'/'.join(str(k) for k in c.key)}："
                  f"变了 {fields} {tail}{hint}", file=sys.stderr)


def open_dataset(args, card, channel: str | None = None, raw: str = ""):
    """(committer or None, start_page, append_from_here).

    Returns None for the committer whenever there is nowhere to write or the
    caller asked for a preview. `start_page` is 1 when there is no dataset.

    `raw` is the capture text when the caller already has it (one-shot `run`).
    Given that, a capture identical to an already-published page is answered
    directly -- "this text is page N" -- instead of asking the user to declare
    where the interface is, because for that case there is no ambiguity at all.
    """
    path = getattr(args, "dataset", None)
    append_from_here = bool(getattr(args, "append_from_here", False))
    if not path:
        return None, 1, append_from_here
    if getattr(args, "dry_run", False):
        # A preview that creates the directory would still be a write. Say so
        # once, plainly, instead of silently reporting as if it had committed.
        print(f"预览：不写 {path}（--dry-run 跳过整个提交步骤）", file=sys.stderr)
        return None, 1, append_from_here

    from .dataset import dataset_from_card

    ds = dataset_from_card(
        card, path,
        allow_card_change=bool(getattr(args, "allow_card_change", False)))
    if ds.state.get("card") and ds.state["card"] != card.name:
        raise InputError(
            f"这个 dataset 是用卡 {ds.state['card']!r} 建的，不是 {card.name!r}",
            hint="换一个 --dataset 目录，或者删掉旧目录重新开始")

    same = ds.page_with_text(raw) if raw else None
    if same is not None:
        raise InputError(
            f"这段原文已经作为第 {same} 页提交过了，再提交一次不会增加任何记录",
            hint=f"要看已有的表：clip2table export -d {path} -f csv")

    if raw:
        # The caller handed us the text itself, so there is no interface position
        # to be wrong about: this page's content is known exactly, and the only
        # thing the page counter has to get right is "one more than what is
        # already published". The resume gate exists for live captures, where we
        # would otherwise write whatever happens to be on screen onto page N+1.
        start_page = int(ds.state.get("pages_done") or 0) + 1
        if start_page > 1:
            print(f"续采：这段文本作为第 {start_page} 页追加"
                  f"（已有 {len(ds.rows)} 行）", file=sys.stderr)
        return Committer(ds, card.name), start_page, append_from_here

    start_page = 1
    from .harvest import can_restore
    can, why_not = can_restore(card)
    plan = ds.resume_plan(channel or card.channel or "",
                          restorable=can, why_not="" if can else why_not)
    if plan.get("needs_explicit_append") and not append_from_here:
        # Refusing to guess is the feature. Advancing the page counter while the
        # UI sits wherever the user left it writes today's screen onto page N+1:
        # data that is conserved, deduped, 100% filled, and from the wrong part
        # of the list. No later check can undo that.
        raise InputError(
            plan["why"],
            hint="确认目标窗口已经显示到该继续的位置，再加 --append-from-here")
    if plan.get("resume"):
        start_page = plan["next_page"]
        print(f"续采：{plan['why']}（已有 {len(ds.rows)} 行）", file=sys.stderr)
    elif append_from_here:
        start_page = int(ds.state.get("pages_done") or 0) + 1
        print(f"按「从当前屏幕接着采」处理：已提交到第 {start_page - 1} 页，"
              f"这一段从第 {start_page} 页记", file=sys.stderr)
    return Committer(ds, card.name), start_page, append_from_here


def run_report_fields(ds, totals: Totals, dry_run: bool) -> dict[str, Any]:
    """The numbers a report needs, named so that two of them cannot collide.

    `this_run` counts what this command did. `dataset_rows` counts the whole
    directory. Printing one where the other is meant is how a 3-row run looks
    like a 200-row dataset, or the reverse.
    """
    out: dict[str, Any] = {"this_run": totals.as_dict(),
                           "this_run_rows": totals.added}
    if ds is not None:
        out["dataset"] = str(ds.path)
        out["dataset_rows"] = len(ds.rows)
        out["committed_pages"] = len(ds.committed_pages())
        if ds.on_collision:
            out["on_collision"] = ds.on_collision
    elif dry_run:
        out["dry_run"] = True
    return out
