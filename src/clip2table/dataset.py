"""dataset: a growing table on disk that survives being killed.

The unit of truth is ONE COMMITTED PAGE, not one file. That distinction is the
whole design. The earlier version wrote four things per page -- raw text,
rows.csv, state.json, manifest.json -- as four separate atomic writes. Each
write was individually safe and the combination was not: kill the process
between the rows.csv replace and the state.json replace and the directory says
"page 3 done" while holding page 2's rows. Nothing about that state looks
broken, which is exactly why it is dangerous.

So a page is committed by building it off to the side and publishing it with a
single rename:

    pages/.tmp-0007/       <- being built; nobody reads this
    pages/0007/            <- published, immutable, never rewritten

A rename is atomic on one filesystem, so a page either exists completely or not
at all. `rows.csv` and `state.json` are then DERIVED from the committed pages
and can be rebuilt at any time with `rebuild()`. If they disagree with `pages/`,
`pages/` wins -- it is the evidence, they are the view.

Layout (plain directory, no database, nothing to corrupt):

    orders/
      pages/
        0001/
          raw.txt        exact source text of page 1
          meta.json      provenance, card snapshot, checkpoint, fingerprint
          rows.json      typed values + per-field provenance
      rows.csv           the table so far (derived, rebuildable)
      state.json         seen keys, pages committed (derived, rebuildable)
      manifest.json      one entry per harvest run
      raw/               legacy pre-page-commit layout, still readable

Resume position is per channel, because "page 4" means different things to
different channels:

  * http    - `page` is a request parameter. Saving it is enough to resume.
  * browser - the address is knowable (CDP reads `location.href`), so the
              checkpoint records the URL actually landed on.
  * desktop - NO address is knowable and the window's scroll position is not
              recoverable by any Win32 call this tool makes. A page number
              therefore does NOT mean "the UI is back where it was", and
              pretending otherwise is how you silently re-collect page 1 while
              writing it to disk as page 4.

When the channel cannot restore the UI, resuming requires `append_from_here`,
which says so in the manifest instead of fabricating continuity. See
`checkpoint_of()` and `resume_plan()`.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError
from .models import ParseResult, Row, _nonempty

ROWS = "rows.csv"
STATE = "state.json"
MANIFEST = "manifest.json"
CONFLICTS = "conflicts.json"
PAGES = "pages"
RAW = "raw"          # legacy layout, read-only from now on
PAGE_COL = "_page"
IDENTITY_COL = "_identity"   # marks a row whose composite key was incomplete
BASELINE_PAGE = 0     # the one immutable page that a legacy rows.csv becomes
DUP_COL = "_dup_of"
SEGMENT_COL = "_segment"
# Field provenance travels with the committed record, not with the CSV view.
# It has to be in `rows.json` because that file is the evidence: a CSV column
# list can be regenerated, but "this cell is empty because the regex missed"
# cannot be recovered once it has been flattened into an empty string.
TRACES_COL = "_traces"
TYPES_COL = "_types"

# What each channel can prove about "where I stopped". Anything not listed as
# restorable must be resumed explicitly, not by arithmetic on a page number.
_RESTORABLE = {"http": True, "browser": True, "desktop": False}


@dataclass
class Conflict:
    """Same dedupe key, different content. A human decision, surfaced not hidden.

    `changed` says which reading this is likely to be. A key that reappears with
    a new price is usually the record UPDATED; a key that reappears with a
    completely different title is usually two different things that happen to
    share a wrong key. The two need opposite fixes -- update the row vs pick a
    better key -- so collapsing them into "duplicate" hides the decision.
    """

    key: tuple
    kept: dict[str, Any]
    rejected: dict[str, Any]
    page: int | None
    changed_fields: list[str] = field(default_factory=list)
    verdict: str = "change"     # "change" | "identity_conflict"
    hint: str = ""              # why, when the verdict is not the whole story

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": [None if k is None else str(k) for k in self.key],
            "page": self.page,
            "verdict": self.verdict,
            "changed_fields": list(self.changed_fields),
            "hint": self.hint,
            "kept": {k: v for k, v in self.kept.items() if not k.startswith("_")},
            "rejected": {k: v for k, v in self.rejected.items() if not k.startswith("_")},
        }


@dataclass
class AppendOutcome:
    added: int = 0
    duplicates: int = 0
    conflicts: int = 0           # kept = the whole bucket, for old callers
    empty_keys: int = 0
    details: list[Conflict] = field(default_factory=list)
    collided: list[Row] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(1 for c in self.details if c.verdict == "change")

    @property
    def identity_conflicts(self) -> int:
        return sum(1 for c in self.details if c.verdict == "identity_conflict")

    @property
    def total_rows(self) -> int:
        return self.added

    def summary(self, on_collision: str) -> str:
        bits = [f"新增 {self.added} 行"]
        if self.duplicates:
            bits.append(f"重复 {self.duplicates} 行已跳过")
        if self.changed:
            bits.append(f"内容变更 {self.changed} 行（{on_collision}）")
        if self.identity_conflicts:
            bits.append(f"身份冲突 {self.identity_conflicts} 行"
                        f"（身份字段变了，{on_collision}）")
        if self.empty_keys:
            bits.append(f"{self.empty_keys} 行去重键不完整，"
                        f"身份未确定（保留，标为 {IDENTITY_COL}=未确定）")
        return "，".join(bits)


@dataclass
class PageCommit:
    """The result of publishing one page. `published` is False when idempotent."""

    page: int
    published: bool
    outcome: AppendOutcome
    path: Path | None = None
    # Set when this page's text turned out to be byte-identical to a page that
    # was already published. Same text is the same source reading; publishing it
    # again would double the evidence for identical rows and move the resume
    # position past data that never arrived.
    duplicate_of: int | None = None


def _key_of(row: Row, keys: list[str]) -> tuple:
    out = []
    for k in keys:
        v = row.values.get(k)
        out.append(v.strip() if isinstance(v, str) else v)
    return tuple(out)


def _has_key(key: tuple) -> bool:
    """A key is usable only when EVERY part is present.

    `any` let a composite key like 公司+订单号 match on 公司 alone: several rows
    with a missing 订单号 fell into one bucket and were merged as if they were
    the same record. For a single-part key `all` and `any` agree, so this only
    tightens the case that was wrong.
    """
    return bool(key) and all(k not in (None, "") for k in key)


_BOOKKEEPING = {PAGE_COL, DUP_COL, SEGMENT_COL, TRACES_COL, IDENTITY_COL}


def _same(a: dict[str, Any], b: dict[str, Any], keys: list[str]) -> bool:
    """Equal apart from the key itself and our own bookkeeping columns."""
    skip = set(keys) | _BOOKKEEPING
    names = (set(a) | set(b)) - skip
    for n in names:
        va, vb = a.get(n), b.get(n)
        if isinstance(va, str) and isinstance(vb, str):
            if va.strip() != vb.strip():
                return False
        elif va != vb:
            return False
    return True


def _diff_fields(a: dict[str, Any], b: dict[str, Any], keys: list[str]) -> list[str]:
    """Which non-key fields disagree between two rows with the same key."""
    skip = set(keys) | _BOOKKEEPING
    out = []
    for n in sorted((set(a) | set(b)) - skip):
        va, vb = a.get(n), b.get(n)
        if isinstance(va, str) and isinstance(vb, str):
            same = va.strip() == vb.strip()
        else:
            same = va == vb
        if not same:
            out.append(n)
    return out


def _comparable_fields(a: dict[str, Any], b: dict[str, Any],
                       keys: list[str]) -> int:
    """How many fields a verdict may be based on: the card's own, minus the key.

    Counting by set difference was wrong -- subtracting the union's size from
    the union's size gives zero, which quietly made every collision look like a
    single-field update and never reported an identity clash at all.
    """
    skip = set(keys) | _BOOKKEEPING
    return len((set(a) | set(b)) - skip)


def _verdict_of(diff: list[str], total: int,
                identity: list[str] | tuple = (),
                mutable: list[str] | tuple = ()) -> tuple[str, str]:
    """Is this the same record updated, or two records sharing a wrong key?

    Returns (verdict, hint). The verdict is "identity_conflict" only on
    evidence the CARD supplied: a field the card declared as part of the
    record's identity has changed, so these are probably two different records
    that happen to share a key.

    A high share of changed columns is NOT such evidence. One order can change
    status, amount and date at once and still be the same order; two different
    records can differ in a single field. So the ratio is returned as a hint
    that the key may be wrong, never as a verdict -- reporting it as an identity
    clash either buries real ones in noise or deletes a record nobody sees.
    """
    ident = set(identity)
    mut = set(mutable)
    if ident:
        hit = [f for f in diff if f in ident]
        if hit:
            return ("identity_conflict",
                    f"卡里声明为身份字段的 {hit} 变了，可能不是同一条记录")
        return "change", ""
    if mut and set(diff) <= mut:
        return "change", ""
    if total > 0 and len(diff) >= 2 and len(diff) / total > 0.5:
        return ("change",
                f"{len(diff)}/{total} 个字段不同，主键可能选错（仅提示，"
                f"未判为身份冲突）")
    return "change", ""


def _dump(obj: Any, path: Path) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _conflict_entry(c: "Conflict", page: int) -> dict[str, Any]:
    """One conflict as it is stored: on the page, and in the legacy aggregate."""
    entry = c.as_dict()
    entry["page"] = page
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return entry


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _cast(raw: Any, typ: str) -> Any:
    """Restore a CSV cell's declared type. Empty stays empty, never 0.

    `int` is parsed as an integer first. The old `int(float(s))` round-trip lost
    precision above 2**53 -- a 19-digit order number came back off by one, and
    the export then disagreed with the page for no visible reason.
    """
    if raw in (None, ""):
        return None
    s = str(raw)
    try:
        if typ == "int":
            t = s.strip()
            if not t:
                return None
            try:
                return int(t)
            except ValueError:
                # "42.0" is what a spreadsheet round-trip produces for an int.
                return int(float(t))
        if typ == "float":
            return float(s)
        if typ == "bool":
            low = s.strip().lower()
            if low in ("true", "1", "yes", "是", "有"):
                return True
            if low in ("false", "0", "no", "否", "无", "没"):
                return False
            return s
    except ValueError:
        # A value that cannot be cast is the value, not a zero.
        return s
    return s


class Dataset:
    """A directory of committed pages, plus derived views. Safe to kill and reopen."""

    def __init__(self, path: str | Path, fields: list[str] | None = None,
                 keys: list[str] | None = None, on_collision: str = "report_only",
                 field_types: dict[str, str] | None = None,
                 identity_fields: list[str] | None = None,
                 mutable_fields: list[str] | None = None):
        self.path = Path(path)
        self.fields = list(fields or [])
        self.keys = list(keys or [])
        self.on_collision = on_collision
        # Which fields the card declares as the record's identity, and which it
        # allows to change while the record stays the same. Both come from the
        # card; without them a changed price and a wrong key look identical.
        self.identity_fields = list(identity_fields or [])
        self.mutable_fields = list(mutable_fields or [])
        # name -> declared type. Needed because rows.csv cannot carry types:
        # "42" round-trips as a string, and a table exported from the dataset
        # then means something different from the same table exported straight
        # from a parse. Pages keep typed JSON; this map restores types when a
        # directory predates page commits and only rows.csv survives.
        self.field_types: dict[str, str] = dict(field_types or {})
        # name -> {type, rule}: the card's rules as they were when the data was
        # taken. Stored because a card edited later must not silently reinterpret
        # rows that were parsed under the old rules.
        self.field_specs: list[dict[str, str]] = []
        self.rows: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}
        self._index: dict[tuple, int] = {}
        self._versions: dict[tuple, list[int]] = {}
        # page -> sha256 of its raw.txt. Dedupe used to re-read and re-hash every
        # committed page's text on every commit, so an N-page harvest read the
        # equivalent of N^2/2 pages off disk just to answer "have I seen this
        # screen before". Each page already records its hash in meta.json, so it
        # is loaded once and kept in step as pages are published.
        self._page_hashes: dict[int, str] | None = None
        # Whether rows.csv (a derived view) still matches self.rows. Lets a
        # commit that changed nothing skip rewriting the whole table.
        self._rows_dirty = True

    # ---------------------------------------------------------------- io

    @property
    def pages_dir(self) -> Path:
        return self.path / PAGES

    @property
    def raw_dir(self) -> Path:
        return self.path / RAW

    def exists(self) -> bool:
        return (self.path / ROWS).is_file() or (self.path / STATE).is_file() or (self.path / "dataset.json").is_file()

    def open(self) -> "Dataset":
        """Load committed pages. Falls back to rows.csv for pre-page-commit dirs."""
        if (self.path / "dataset.json").is_file():
            return self._open_v2()
        if (self.path / STATE).is_file():
            self.state = _load(self.path / STATE, {})
        if not self.fields:
            self.fields = list(self.state.get("fields") or [])
        if not self.keys:
            self.keys = list(self.state.get("keys") or [])
        if not self.field_types:
            loaded = self.state.get("field_types") or {}
            if isinstance(loaded, dict):
                self.field_types = {str(k): str(v) for k, v in loaded.items()}
        if not self.field_specs:
            loaded_specs = self.state.get("field_specs") or []
            if isinstance(loaded_specs, list):
                self.field_specs = [dict(s) for s in loaded_specs if isinstance(s, dict)]
        if self.state.get("on_collision"):
            self.on_collision = str(self.state["on_collision"])

        committed = self.committed_pages()
        if committed:
            # Pages are the evidence: rebuild the table from them rather than
            # trusting rows.csv. A directory whose views drifted must snap back
            # to what was actually committed.
            #
            # State comes from the pages too, BEFORE the replay. Rebuilding the
            # rows and leaving `pages_done` in state.json meant a crash between
            # "page published" and "state written" reopened as "records are
            # here, resume position is not": the next run would re-fetch page N
            # and publish it as N+1. state.json is a cache; the pages are truth.
            self._restore_from_pages(committed)
            self._load_from_pages(committed)
        elif (self.path / ROWS).is_file():
            self.rows = self._read_rows()
            # The view on disk is where these rows came from, so it is in step.
            self._rows_dirty = False
        self._reindex()
        return self

    def _open_v2(self):
        """Compatibility view, derived exclusively from the selected v2 revision."""
        from . import revisions, api
        revisions.init(self.path)
        rid = revisions.active(self.path).get("revision_id")
        if not rid:
            return self
        rev = revisions.read_revision(self.path, rid)
        self.rows = []
        self.fields = []
        for e in rev.entries:
            _, res = revisions.read_parse(self.path, e["parse_id"])
            self.fields = list(dict.fromkeys(self.fields + res.fields))
        policy = api.default_policy(self.path, rid)
        self.keys = list(policy.keys)
        self.identity_fields, self.mutable_fields = list(policy.identity), list(policy.mutable)
        if self.keys:
            rows, _, _ = api.ledger_view(self.path, rid)
            self.rows = [{f: r.get(f) for f in self.fields} for r in rows]
        else:
            self.rows = [dict(r.values) for e in rev.entries for r in revisions.read_parse(self.path, e["parse_id"])[1].rows]
        self.state = {"pages_done": len(rev.entries), "fields": self.fields, "keys": self.keys,
                      "revision": rid, "layout": 2}
        self._reindex()
        self._rows_dirty = False
        return self

    def _restore_from_pages(self, pages: list[int]) -> None:
        """Fill in fields/keys/policy/position from the page metas.

        The last page wins, because a dataset's definition is whatever the most
        recent page was actually parsed with. Values already supplied by the
        caller (a live run passes the card's) are not overwritten -- the card
        drift check is what guards that path.
        """
        metas: list[dict[str, Any]] = []
        for page in pages:
            meta = _load(self.pages_dir / f"{page:04d}" / "meta.json", {})
            if isinstance(meta, dict) and meta:
                metas.append(meta)
        if not metas:
            return
        last = metas[-1]
        if not self.fields and last.get("fields"):
            self.fields = list(last["fields"])
        if not self.keys and last.get("keys") is not None:
            self.keys = list(last["keys"])
        if last.get("on_collision"):
            self.on_collision = str(last["on_collision"])
        if not self.field_types:
            types: dict[str, str] = {}
            for meta in metas:
                got = meta.get("field_types")
                if isinstance(got, dict):
                    types.update({str(k): str(v) for k, v in got.items()})
            self.field_types = types
        if not self.field_specs:
            specs: list[dict[str, Any]] = []
            for meta in metas:
                got = meta.get("field_specs")
                if isinstance(got, list):
                    specs = [dict(s) for s in got if isinstance(s, dict)]
            self.field_specs = specs
        for key in ("card", "card_snapshot"):
            if not self.state.get(key) and last.get(key) is not None:
                self.state[key] = last[key]
        # Position: the highest published page, not what the cache happens to say.
        real = [p for p in pages if p != BASELINE_PAGE]
        self.state["pages_done"] = max(real, default=0)
        self.state["checkpoint"] = self.checkpoint_of(
            str(last.get("channel") or ""), max(real, default=0))

    # -------------------------------------------------- committed pages

    def committed_pages(self) -> list[int]:
        """Page numbers that exist as complete, published directories."""
        if (self.path / "dataset.json").is_file():
            from . import revisions
            rid = revisions.active(self.path).get("revision_id")
            return list(range(1, len(revisions.read_revision(self.path, rid).entries) + 1)) if rid else []
        d = self.pages_dir
        if not d.is_dir():
            return []
        out = []
        for child in sorted(d.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # A published page always has all three files; a directory that is
            # missing one was not published by us and must not be counted as
            # committed, or the resume position would skip real work.
            if all((child / n).is_file() for n in ("raw.txt", "meta.json", "rows.json")):
                try:
                    out.append(int(child.name))
                except ValueError:
                    continue
        return out

    def page_numbers(self) -> list[int]:
        """Committed pages that came from a real fetch, i.e. not the baseline.

        The baseline is a page on disk (same layout, same replay path) but it is
        not a place in the site. Counting it as one would make `pages_done`
        claim a fetch that never happened, and the next run would start at
        page 2 having only ever seen page 1's worth of history.
        """
        return [p for p in self.committed_pages() if p != BASELINE_PAGE]

    def _load_from_pages(self, pages: list[int]) -> None:
        """Rebuild in-memory rows by REPLAYING the merge, not concatenating.

        Each page's `rows.json` holds every record that page produced, including
        the ones that lost a dedupe collision. Concatenating them would resurrect
        the losers -- the table would grow every time it was rebuilt, which is
        the opposite of a rebuild. So the pages are folded through the same
        `_merge` the live run used, keys and all.
        """
        if not self.fields:
            for page in pages:
                meta = _load(self.pages_dir / f"{page:04d}" / "meta.json", {})
                if meta.get("fields"):
                    self.fields = list(meta["fields"])
                    break
        self.rows = []
        self._index = {}
        # Rows are about to be rebuilt from the pages, so whatever rows.csv holds
        # is stale by definition and the next save must rewrite it.
        self._rows_dirty = True
        for page in pages:
            records = _load(self.pages_dir / f"{page:04d}" / "rows.json", [])
            baseline = page == BASELINE_PAGE
            rows = []
            for rec in records:
                clean = {k: v for k, v in rec.items()
                         if k not in ("_provenance", "_segment", TRACES_COL)}
                # The baseline's rows came from an old rows.csv, so their real
                # page number is whatever that file said -- not 0. Passing None
                # makes _merge use row.source_page instead of stamping 0 over
                # it, which would erase where the data actually came from.
                src = rec.get(PAGE_COL)
                rows.append(Row(values=clean,
                                source_page=None if baseline else page,
                                traces=self._unpack_traces(rec)))
                if baseline:
                    try:
                        rows[-1].source_page = int(src) if src not in (None, "") else None
                    except (TypeError, ValueError):
                        rows[-1].source_page = None
            self._merge(ParseResult(rows=rows, fields=list(self.fields)),
                        None if baseline else page)

    def read_page(self, page: int) -> dict[str, Any]:
        """Everything committed for one page, for inspection and re-export."""
        d = self.pages_dir / f"{page:04d}"
        if not d.is_dir():
            raise InputError(f"第 {page} 页没有提交过", hint=str(d))
        return {
            "page": page,
            "raw": (d / "raw.txt").read_text(encoding="utf-8"),
            "meta": _load(d / "meta.json", {}),
            "rows": _load(d / "rows.json", []),
        }

    def pages_meta(self) -> list[dict[str, Any]]:
        return [dict(_load(self.pages_dir / f"{p:04d}" / "meta.json", {}), page=p)
                for p in self.committed_pages()]

    # -------------------------------------------------------- resume plan

    def checkpoint_of(self, channel: str, page: int) -> dict[str, Any]:
        """What this channel can honestly say about where it stopped."""
        return {
            "page": page,
            "channel": channel,
            "ui_restorable": bool(_RESTORABLE.get(channel, False)),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def resume_plan(self, channel: str,
                    restorable: bool | None = None,
                    why_not: str = "") -> dict[str, Any]:
        """Decide how a fresh run continues this dataset.

        The arithmetical branch (`pages_done + 1`) is only offered when the
        channel can actually put itself back. For desktop there is no such
        thing -- the window's position belongs to the user -- so the plan says
        `needs_explicit_append` and the CLI refuses to guess.

        `restorable` overrides the per-channel default because resumability is a
        property of the CARD: a browser card whose list has an addressable page
        URL can be navigated back, one whose URL never changes cannot. Defaulting
        to "browser can resume" made the documentation claim more than the code
        did.
        """
        done = int(self.state.get("pages_done") or 0)
        if not done:
            if self.rows:
                # A migrated legacy dataset: history is here, but no page of it
                # was ever fetched through this pipeline, so there is no page
                # number to continue from. Start numbering at 1 again; the
                # baseline stays as page 0 and the two never collide.
                return {"next_page": 1, "resume": False,
                        "why": (f"这个 dataset 的历史（{len(self.rows)} 行）来自旧 "
                                f"rows.csv，已存为不可变基线（第 0 页）。它没有原文、"
                                f"没有字段溯源，不能当作已采集的页来续。新数据从第 1 "
                                f"页开始编号")}
            return {"next_page": 1, "resume": False, "why": "这个 dataset 还是空的"}
        restorable = (bool(_RESTORABLE.get(channel, False))
                      if restorable is None else bool(restorable))
        if restorable:
            return {"next_page": done + 1, "resume": True,
                    "why": f"第 {done} 页已提交，从第 {done + 1} 页继续"}
        if why_not:
            return {
                "next_page": done + 1,
                "resume": False,
                "needs_explicit_append": True,
                "why": (f"第 {done} 页已提交，但{why_not}。页号前移并不代表界面"
                        f"回到了那里——直接接着采会把当前屏幕上看到的内容记成第 "
                        f"{done + 1} 页。要接着采，请显式加 --append-from-here"),
            }
        return {
            "next_page": done + 1,
            "resume": False,
            "needs_explicit_append": True,
            "why": (f"第 {done} 页已提交。但 {channel} 通道读不到当前界面位置，"
                    f"页号前移并不代表界面回到了那里——直接接着采会把当前屏幕上"
                    f"看到的内容记成第 {done + 1} 页。要接着采，请显式加 "
                    f"--append-from-here，我才会把它作为新一段接在后面"),
        }

    # ------------------------------------------------------------ append

    def _stage_page(self, page: int, raw: str, meta: dict[str, Any],
                    rows: list[dict[str, Any]],
                    outcome: AppendOutcome,
                    deviations: list[str] | None = None,
                    details: list[Conflict] | None = None) -> Path:
        """Build the page directory somewhere nobody reads. Returns its path."""
        d = self.pages_dir
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".tmp-{page:04d}-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        (tmp / "raw.txt").write_text(raw, encoding="utf-8")
        payload = dict(meta)
        payload.update({
            "page": page,
            "chars": len(raw),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "fields": list(self.fields),
            "keys": list(self.keys),
            "on_collision": self.on_collision,
            "row_count": len(rows),
            "added": outcome.added,
            "duplicates": outcome.duplicates,
            "conflicts": outcome.conflicts,
            "empty_keys": outcome.empty_keys,
            "committed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # The definition this page was parsed with, so a dataset can be
            # rebuilt from pages alone -- keys, merge policy and types included.
            "field_types": dict(self.field_types),
            "field_specs": [dict(s) for s in self.field_specs],
            "card": self.state.get("card") or "",
            "card_snapshot": self.state.get("card_snapshot"),
        })
        if deviations:
            # A page that missed what the card promised is still published --
            # dropping it would lose real rows -- but it is marked, so a later
            # reader can find the pages that need re-checking without diffing
            # every page's text against the card by hand.
            payload["deviations"] = list(deviations)
        _dump(payload, tmp / "meta.json")
        _dump(rows, tmp / "rows.json")
        if details:
            # Conflict detail belongs to the page that produced it, written in
            # the same atomic publish. Keeping it in a separate top-level file
            # written after the rename left a window: page published, conflict
            # file not yet updated, and the only record of "this key matched two
            # different things" was gone for good.
            _dump([_conflict_entry(c, page) for c in details],
                  tmp / "conflicts.json")
        return tmp

    def commit_page(self, page: int, raw: str, meta: dict[str, Any],
                    res: ParseResult,
                    append_from_here: bool = False) -> PageCommit:
        """Merge one parsed page and publish it atomically.

        Order is the point: compute, stage, rename, then advance the position.
        Nothing outside `pages/` moves until the page is fully on disk, so a
        crash at any moment costs at most the page in flight.
        """
        target = self.pages_dir / f"{page:04d}"
        if target.is_dir():
            # Already published. Re-running a page must not double-count, and
            # must not rewrite evidence that is supposed to be immutable.
            return PageCommit(page=page, published=False,
                              outcome=AppendOutcome(), path=target)
        same = self.page_with_text(raw)
        if same is not None:
            # The same bytes are already evidence for some page. Publishing them
            # again would double the row count for one screen and advance the
            # position past content that never arrived.
            return PageCommit(page=page, published=False,
                              outcome=AppendOutcome(),
                              path=self.pages_dir / f"{same:04d}",
                              duplicate_of=same)

        # A dataset that predates pages/ has its history only in rows.csv.
        # Freezing that history into an immutable baseline page BEFORE merging
        # means the first new page is replayed after it, so the old rows keep
        # their place in every future rebuild. Without this the first append
        # writes a page holding only the new rows, and the next open (which
        # replays pages and ignores rows.csv) silently drops the history.
        self._ensure_baseline()

        outcome = self._merge(res, page)
        records = []
        for row in res.rows:
            rec = dict(row.values)
            rec.setdefault(PAGE_COL, page)
            prov = getattr(row, "provenance", None)
            if prov:
                rec["_provenance"] = prov
            if row.traces:
                rec[TRACES_COL] = self._pack_traces(row)
            if append_from_here:
                rec["_segment"] = f"append@{page}"
            records.append(rec)

        staged = self._stage_page(page, raw, meta, records, outcome,
                                  deviations=list(res.deviations),
                                  details=list(outcome.details))
        try:
            os.replace(staged, target)      # atomic publish on one filesystem
        except OSError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        if self._page_hashes is not None:
            # Keep the dedupe cache in step with the disk instead of dropping it:
            # the next page of the same run is exactly the one that needs it.
            self._page_hashes[page] = hashlib.sha256(
                raw.encode("utf-8")).hexdigest()
        self._advance(page, meta, append_from_here)
        if outcome.details:
            # The top-level file is a CACHE of what the pages already say, so a
            # reader that only looks there still sees the full history. It is
            # rebuilt, never appended to -- appending is what made a crash
            # between publish and append lose an entry permanently.
            self._sync_conflicts_cache()
        return PageCommit(page=page, published=True, outcome=outcome, path=target)

    def _ensure_baseline(self) -> None:
        """Freeze a legacy rows.csv into page 0, once, before the first append.

        Only fires for a dataset that has history but no fetched pages yet. The
        baseline is written with the same atomic publish as any page, so a crash
        mid-way leaves either the old dataset untouched or a complete baseline.
        """
        if self.page_numbers() or not self.rows:
            return
        if not (self.path / ROWS).is_file():
            return
        d = self.pages_dir / f"{BASELINE_PAGE:04d}"
        if d.is_dir():
            return
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.pages_dir / f".tmp-baseline-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        records = [dict(r) for r in self.rows]
        # No raw.txt content: the original page text was never stored, and
        # inventing it would make the baseline look like evidence it is not.
        (tmp / "raw.txt").write_text("", encoding="utf-8")
        _dump({
            "page": BASELINE_PAGE,
            "baseline": True,
            "baseline_source": ROWS,
            "raw_available": False,
            "note": ("历史基线：由旧 rows.csv 迁移而来。原文、字段溯源、"
                     "页面元数据在旧格式里从未保存，这里不补造。"),
            "chars": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "fields": list(self.fields),
            "keys": list(self.keys),
            "on_collision": self.on_collision,
            "field_types": dict(self.field_types),
            "field_specs": [dict(s) for s in self.field_specs],
            "card": self.state.get("card") or "",
            "card_snapshot": self.state.get("card_snapshot"),
            "row_count": len(records),
            "added": len(records),
            "duplicates": 0,
            "conflicts": 0,
            "empty_keys": 0,
            "committed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, tmp / "meta.json")
        _dump(records, tmp / "rows.json")
        try:
            os.replace(tmp, d)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def page_with_text(self, raw: str) -> int | None:
        """The published page (if any) whose raw.txt is exactly this text.

        Same text is not a coincidence to be merged away -- it is the same
        screen being committed twice. Catches the `run`-twice case and the
        stuck-pager case that the page fingerprint also catches at fetch time;
        here it is the last line of defence, after parsing already happened.
        """
        if not raw:
            return None
        want = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        for page, got in self._known_page_hashes().items():
            if got == want:
                return page
        return None

    def _known_page_hashes(self) -> dict[int, str]:
        """page -> sha256(raw.txt), loaded once from meta.json and then cached.

        Reading every page's text to answer "is this screen new?" made the check
        quadratic in the number of pages. meta.json already stores the hash, so
        the cost is paid once per open, not once per page. A page whose meta has
        no usable hash is hashed from its raw.txt and remembered here.
        """
        if self._page_hashes is None:
            cache: dict[int, str] = {}
            for page in self.committed_pages():
                meta = _load(self.pages_dir / f"{page:04d}" / "meta.json", {})
                sha = meta.get("sha256") if isinstance(meta, dict) else None
                if isinstance(sha, str) and sha:
                    cache[page] = sha
            self._page_hashes = cache
        for page in self.committed_pages():
            if page in self._page_hashes:
                continue
            p = self.pages_dir / f"{page:04d}" / "raw.txt"
            try:
                self._page_hashes[page] = hashlib.sha256(
                    p.read_text(encoding="utf-8", errors="replace")
                    .encode("utf-8")).hexdigest()
            except OSError:
                continue
        return self._page_hashes

    def read_conflicts(self) -> list[dict[str, Any]]:
        """Every key collision ever committed, newest last.

        Rebuilt from the pages, in page order. The old implementation appended
        to a top-level conflicts.json after publishing the page, so a crash in
        between lost the detail while keeping the page -- the rows were there
        and the reason two of them disagreed was not.

        A directory written before per-page conflicts still has the old
        aggregate file and no per-page files; that file is used as-is so old
        datasets do not silently lose their history.
        """
        out: list[dict[str, Any]] = []
        for page in self.committed_pages():
            got = _load(self.pages_dir / f"{page:04d}" / "conflicts.json", [])
            if isinstance(got, list):
                out.extend(e for e in got if isinstance(e, dict))
        if out:
            return out
        legacy = _load(self.path / CONFLICTS, [])
        return legacy if isinstance(legacy, list) else []

    def _sync_conflicts_cache(self) -> None:
        """Rewrite the aggregate conflicts file from the pages. Derived, not truth."""
        entries = self.read_conflicts()
        path = self.path / CONFLICTS
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        _dump(entries, tmp)
        os.replace(tmp, path)

    def _record_conflicts(self, page: int, details: list[Conflict]) -> None:
        """Append to the legacy aggregate file. Kept for old callers only."""
        path = self.path / CONFLICTS
        kept = _load(path, [])
        if not isinstance(kept, list):
            kept = []
        for c in details:
            kept.append(_conflict_entry(c, page))
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        _dump(kept, tmp)
        os.replace(tmp, path)

    @staticmethod
    def _pack_traces(row: Row) -> dict[str, Any]:
        return {name: t.as_dict() for name, t in row.traces.items()}

    def _unpack_traces(self, rec: dict[str, Any]) -> dict[str, Any]:
        raw = rec.get(TRACES_COL) or {}
        if not isinstance(raw, dict):
            return {}
        from .models import FieldTrace
        out = {}
        for name, d in raw.items():
            if isinstance(d, dict):
                try:
                    out[str(name)] = FieldTrace.from_dict(str(name), d)
                except TypeError:
                    continue
        return out

    def _advance(self, page: int, meta: dict[str, Any],
                 append_from_here: bool) -> None:
        """Move the resume position forward. Only after the page is published."""
        self.state["pages_done"] = page
        self.state["checkpoint"] = self.checkpoint_of(
            str(meta.get("channel") or ""), page)
        if append_from_here:
            self.state.setdefault("appended", []).append(page)
        self.state.setdefault("last_provenance", {})
        self.state["last_provenance"] = {
            "requested_url": meta.get("requested_url"),
            "observed_url": meta.get("observed_url"),
            "channel": meta.get("channel"),
            "engine": meta.get("engine"),
        }
        self.save()

    def _merge(self, res: ParseResult, page: int | None) -> AppendOutcome:
        """Fold one page's rows into `self.rows`. Pure in-memory, no disk."""
        out = AppendOutcome()
        fields_changed = False
        if not self.fields and res.fields:
            self.fields = list(res.fields)
            fields_changed = True
        for row in res.rows:
            values = dict(row.values)
            if row.traces:
                values[TRACES_COL] = self._pack_traces(row)
            page_no = page if page is not None else row.source_page
            if page_no is not None:
                values[PAGE_COL] = page_no
            if not self.keys:
                self.rows.append(values)
                out.added += 1
                continue
            key = _key_of(row, self.keys)
            if not _has_key(key):
                # Kept, but it cannot participate in dedupe: we do not know
                # which record it is. Marked so the user can see why the same
                # thing may appear twice instead of wondering.
                out.empty_keys += 1
                values[IDENTITY_COL] = "未确定"
                self.rows.append(values)
                out.added += 1
                continue
            prev = self._index.get(key)
            if prev is None:
                self._index[key] = len(self.rows)
                self._versions.setdefault(key, []).append(len(self.rows))
                self.rows.append(values)
                out.added += 1
                continue
            kept = self.rows[prev]
            if _same(kept, values, self.keys):
                out.duplicates += 1
                continue
            # Same key, different content. Two very different situations wear
            # the same mask here, and telling them apart is the whole point of
            # the four-way split: an updated record and a wrong key both look
            # like "conflict" and need opposite fixes.
            diff = _diff_fields(kept, values, self.keys)
            comparable = _comparable_fields(kept, values, self.keys)
            verdict, hint = _verdict_of(
                diff, comparable,
                identity=list(getattr(self, "identity_fields", []) or []),
                mutable=list(getattr(self, "mutable_fields", []) or []))
            conflict = Conflict(key=key, kept=dict(kept), rejected=values,
                                page=page_no, changed_fields=diff,
                                verdict=verdict, hint=hint)
            out.conflicts += 1
            out.details.append(conflict)
            if self.on_collision == "keep_all":
                # keep_all means "hold every distinct version", not "append
                # whatever arrives". Appending unconditionally made the same
                # second version land again on every page that repeated it.
                if any(_same(self.rows[i], values, self.keys)
                       for i in self._versions.get(key, [])):
                    out.duplicates += 1
                else:
                    values[DUP_COL] = prev + 1
                    self._versions.setdefault(key, []).append(len(self.rows))
                    self.rows.append(values)
                    out.collided.append(row)
                    out.added += 1
            # report_only and keep_first both keep the first value. They differ
            # only in whether the disagreement is shown, and it always is.
        if out.added or fields_changed:
            # A page that was entirely duplicate changes nothing in the table, so
            # rewriting rows.csv for it is pure cost. Marking the view dirty only
            # when the rows really moved is what keeps "one write per page" from
            # becoming "one write per page even when nothing happened".
            self._rows_dirty = True
        return out

    def append(self, res: ParseResult, page: int | None = None) -> AppendOutcome:
        """Merge without publishing. Kept for callers that only want a preview."""
        return self._merge(res, page)

    # ------------------------------------------------------------- views

    def _read_rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with (self.path / ROWS).open("r", encoding="utf-8-sig", newline="") as fh:
            for rec in csv.DictReader(fh):
                row = dict(rec)
                page = row.get(PAGE_COL)
                if page not in (None, ""):
                    try:
                        row[PAGE_COL] = int(page)
                    except ValueError:
                        pass
                for name, typ in self.field_types.items():
                    if name in row:
                        row[name] = _cast(row[name], typ)
                out.append(row)
        return out

    def _reindex(self) -> None:
        self._index = {}
        self._versions = {}
        if not self.keys:
            return
        for i, row in enumerate(self.rows):
            key = tuple(row.get(k) for k in self.keys)
            if _has_key(key):
                self._index.setdefault(key, i)
                self._versions.setdefault(key, []).append(i)

    def save(self) -> None:
        """Write rows.csv, then state.json. Both are derived; pages/ is truth.

        rows.csv is rewritten only when the rows actually moved. A page that was
        all duplicates still advances the position, and rewriting the whole table
        for it would be work with no reader benefit.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        if self._rows_dirty or not (self.path / ROWS).is_file():
            self._write_rows()
            self._rows_dirty = False
        self.state.update({
            "fields": self.fields,
            "keys": self.keys,
            "on_collision": self.on_collision,
            "field_types": self.field_types,
            "field_specs": self.field_specs,
            "rows": len(self.rows),
            "pages_committed": len(self.page_numbers()),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        st = self.path / (STATE + ".tmp")
        _dump(self.state, st)
        os.replace(st, self.path / STATE)

    def rebuild(self) -> int:
        """Recreate rows.csv + state.json from the committed pages.

        Exists because the views are allowed to be wrong and the pages are not.
        Any moment the two disagree, this restores the only version that has
        evidence behind it.
        """
        pages = self.committed_pages()
        self.rows = []
        self._index = {}
        self._load_from_pages(pages)
        self._reindex()
        self.save()
        real = [p for p in pages if p != BASELINE_PAGE]
        if real:
            self.state["pages_done"] = max(real)
            self.save()
        return len(self.rows)

    def _write_rows(self) -> None:
        cols = list(self.fields) + [c for c in (PAGE_COL, DUP_COL, SEGMENT_COL,
                                                IDENTITY_COL)
                                    if self._uses(c)]
        tmp = self.path / (ROWS + ".tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for row in self.rows:
                w.writerow(row)
        os.replace(tmp, self.path / ROWS)

    def _uses(self, col: str) -> bool:
        return any(col in r and r.get(col) not in (None, "") for r in self.rows)

    def write_raw(self, page: int, text: str, meta: dict[str, Any]) -> Path:
        """Legacy hook. Kept so old callers do not break; pages/ replaces it."""
        d = self.raw_dir
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{page:04d}"
        body = d / f"{stem}.txt"
        if not body.exists():
            body.write_text(text, encoding="utf-8")
        out = d / f"{stem}.meta.json"
        payload = dict(meta)
        payload.update({"page": page, "chars": len(text),
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        _dump(payload, out)
        return body

    def log_run(self, entry: dict[str, Any]) -> None:
        path = self.path / MANIFEST
        runs: list[dict[str, Any]] = []
        if path.is_file():
            runs = _load(path, [])
            if not isinstance(runs, list):
                runs = []
        entry = dict(entry)
        entry.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        runs.append(entry)
        self.path.mkdir(parents=True, exist_ok=True)
        _dump(runs, path)

    def as_rows(self) -> list[Row]:
        out = []
        for r in self.rows:
            traces = self._unpack_traces(r)
            out.append(Row(
                values={k: v for k, v in r.items()
                        if k not in (PAGE_COL, DUP_COL, SEGMENT_COL,
                                     TRACES_COL, "_provenance")},
                source_page=r.get(PAGE_COL) if isinstance(r.get(PAGE_COL), int) else None,
                traces=traces))
        return out

    def as_result(self) -> ParseResult:
        return ParseResult(rows=self.as_rows(), fields=list(self.fields), level="dataset")


def _describe_rule(f) -> str:
    """One line that says what this field points AT, not what it is called.

    Written against the FieldSpec fields directly instead of a method on it: a
    snapshot whose rule string is empty for every field would compare equal
    across any edit, which is the exact blindness this check exists to remove.
    """
    kind = getattr(f, "kind", "search")
    bits: list[str] = [str(kind)]
    if kind == "cell":
        bits.append(f"col={getattr(f, 'col', None)}")
    elif kind == "line":
        bits.append(f"index={getattr(f, 'index', 0)}")
    elif kind == "search":
        bits.append(f"pattern={getattr(f, 'pattern', None)!r}")
        scope = getattr(f, "scope", "record")
        bits.append(f"scope={scope}")
        window = getattr(f, "window", None)
        if window is not None:
            bits.append(f"window={window}")
    elif kind == "span":
        bits.append(f"from={getattr(f, 'start', None)!r}")
        bits.append(f"to={getattr(f, 'end', None)!r}")
    elif kind == "dom":
        bits.append(f"sel={getattr(f, 'sel', None)!r}/{getattr(f, 'attr', 'text')}")
    elif kind == "json":
        bits.append(f"path={getattr(f, 'path', None)!r}")
    elif kind == "const":
        bits.append(f"value={getattr(f, 'value', None)!r}")
    for key in ("min_len", "exclude"):
        val = getattr(f, key, None)
        if val:
            bits.append(f"{key}={val!r}")
    if getattr(f, "strip", True) is False:
        bits.append("strip=False")
    return " ".join(bits)


def _field_def(f) -> dict[str, Any]:
    """Every attribute that decides what this field extracts.

    A display summary is not a definition: `line` used to be fingerprinted as
    "index=2" alone, so editing its pattern changed what the column contained
    while the snapshot compared equal. Enumerating the dataclass means a new
    FieldSpec attribute is covered the day it is added, instead of the day
    someone remembers to add it here.
    """
    out: dict[str, Any] = {}
    for fld in dataclasses.fields(f):
        val = getattr(f, fld.name)
        if isinstance(val, (list, tuple)):
            val = list(val)
        out[fld.name] = val
    return out


def _field_specs(card) -> list[dict[str, Any]]:
    out = []
    for f in getattr(card, "fields", []):
        out.append({"name": f.name,
                    "type": getattr(f, "type", "text") or "text",
                    # `rule` is for humans reading meta.json; `def` is what the
                    # drift check compares. Keeping both means the readable
                    # summary never has to double as the definition.
                    "rule": _describe_rule(f),
                    "def": _field_def(f)})
    return out


def _jsonable(obj: Any) -> Any:
    """A TOML-sourced structure, normalised so two equal configs compare equal."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _card_fingerprint(card) -> dict[str, Any]:
    """What a card MEANS, reduced to a comparable snapshot.

    The dataset already refuses a different card *name*. That is not enough: a
    card edited in place keeps its name and changes what every field points at,
    so rows collected before and after describe different things under the same
    column headings. The table then looks homogeneous and is not -- the failure
    mode this project exists to prevent, one level up from the page.

    Everything that can change the RESULT is in here: how records are cut, how
    fields are extracted, what counts as a duplicate, and how far the harvest
    is allowed to run. Paging and harvest settings are included because
    changing them changes which rows exist, not just how they were labelled.
    """
    return {
        "name": getattr(card, "name", ""),
        "channel": getattr(card, "channel", ""),
        "segment": {
            "mode": getattr(card, "segment_mode", ""),
            "anchor": getattr(card, "anchor", None),
            "anchor_next": getattr(card, "anchor_next", None),
            "min_lines": getattr(card, "min_lines", 1),
            "skip_header": bool(getattr(card, "skip_header", False)),
            "record_lines": list(getattr(card, "record_lines", None) or []),
            "required": list(getattr(card, "required", []) or []),
            **({"period": card.segment_period, "offset": card.segment_offset,
                "count": card.segment_count}
               if getattr(card, "segment_mode", "") == "periodic" else {}),
        },
        "dedupe": {
            "keys": list(getattr(card, "dedupe_key", []) or []),
            "on_collision": getattr(card, "on_collision", "report_only"),
            "identity": list(getattr(card, "identity_fields", []) or []),
            "mutable": list(getattr(card, "mutable_fields", []) or []),
        },
        "paging": {
            "style": getattr(card, "paging_style", "none"),
            "next": getattr(card, "paging_next", None),
            "url_template": getattr(card, "paging_url_template", None),
            "max_pages": getattr(card, "max_pages", 1),
            "stop_when": getattr(card, "stop_when", "fingerprint"),
            "stop_after_empty": getattr(card, "stop_after_empty", 2),
        },
        "harvest": _jsonable(getattr(card, "harvest", {}) or {}),
        "fetch": _jsonable(getattr(card, "fetch", {}) or {}),
        "target": _jsonable(getattr(card, "target", {}) or {}),
        "fields": _field_specs(card),
        # `[ledger]` decides which observation counts as current, so it changes
        # the RESULT of a re-read just as much as a field rule does. Leaving it
        # out made a rule snapshot reload with the default `observed` ordering,
        # and the same revision then produced a different "current" table
        # depending on whether the caller passed a card or let the snapshot
        # speak -- i.e. the CLI and the API disagreed.
        "ledger": _jsonable(getattr(card, "ledger", {}) or {}),
    }


_LABELS = {
    "channel": "通道", "name": "卡名",
}


def describe_card_drift(old: dict[str, Any],
                        new: dict[str, Any]) -> list[str]:
    """Which declared rules changed between two snapshots, in plain words."""
    if not old:
        return []
    out: list[str] = []

    for section, label in (("segment", "切分"), ("dedupe", "去重"),
                           ("paging", "翻页"), ("harvest", "采集"),
                           ("fetch", "取数"), ("target", "目标"),
                           ("ledger", "台账")):
        a, b = old.get(section), new.get(section)
        if not isinstance(a, dict) or not isinstance(b, dict):
            if a != b:
                out.append(f"{label}配置：{a!r} → {b!r}")
            continue
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                # The dotted path is what the card file calls it, so the message
                # can be acted on without translating 翻页 back to `paging`.
                out.append(f"{section}.{key}（{label}）："
                           f"{a.get(key)!r} → {b.get(key)!r}")

    for key in ("channel", "name"):
        if old.get(key) != new.get(key):
            out.append(f"{_LABELS.get(key, key)}：{old.get(key)!r} → {new.get(key)!r}")

    old_f = {f["name"]: f for f in old.get("fields") or [] if isinstance(f, dict)}
    new_f = {f["name"]: f for f in new.get("fields") or [] if isinstance(f, dict)}
    for name in sorted(set(old_f) | set(new_f)):
        a, b = old_f.get(name), new_f.get(name)
        if a is None:
            out.append(f"新增字段 {name}")
            continue
        if b is None:
            out.append(f"删掉字段 {name}")
            continue
        da, db = a.get("def") or {}, b.get("def") or {}
        if not da and not db:
            # Snapshot written by an older version: compare what it had.
            if (a.get("rule"), a.get("type")) != (b.get("rule"), b.get("type")):
                out.append(f"字段 {name}：{a.get('rule')!r} → {b.get('rule')!r}")
            continue
        for key in sorted(set(da) | set(db)):
            if da.get(key) != db.get(key):
                out.append(f"字段 {name}.{key}：{da.get(key)!r} → {db.get(key)!r}")
    return out


def dataset_from_card(card, path: str | Path,
                      allow_card_change: bool = False) -> Dataset:
    specs = _field_specs(card)
    ds = Dataset(path, fields=list(card.field_names),
                 keys=list(card.dedupe_key or []),
                 on_collision=card.on_collision or "report_only",
                 field_types={s["name"]: s["type"] for s in specs},
                 identity_fields=list(getattr(card, "identity_fields", []) or []),
                 mutable_fields=list(getattr(card, "mutable_fields", []) or []))
    ds.field_specs = specs
    ds = ds.open()

    now = _card_fingerprint(card)
    was = ds.state.get("card_snapshot")
    drift = describe_card_drift(was, now) if isinstance(was, dict) else []
    if drift and not allow_card_change:
        # Refuse BEFORE touching state: a rejected run must leave the directory
        # exactly as it was, or the next legitimate run compares against a card
        # that was never actually used for any row.
        raise InputError(
            f"这张卡的定义和这个 dataset 采集时用的不一样了（{len(drift)} 处）：\n"
            + "\n".join("  · " + d for d in drift),
            hint="同一个 dataset 里混两种字段定义，表看着齐整、含义已经不同。"
                 "换个 --dataset 目录重采，或者确认要混入后加 --allow-card-change")
    if drift:
        ds.state.setdefault("card_drift", []).append({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "changes": drift,
        })
    changed = ds.state.get("card_snapshot") != now
    ds.state["card_snapshot"] = now
    if changed and ds.path.is_dir():
        # State only: rows.csv is a view of the committed pages, and there are
        # no new pages yet. Writing it here would let a zero-page run rewrite
        # the table the pages currently imply.
        tmp = ds.path / (STATE + ".tmp")
        _dump(ds.state, tmp)
        os.replace(tmp, ds.path / STATE)
    return ds


def require_dir(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_dir():
        raise InputError(f"dataset 目录不存在：{p}",
                         hint="先跑一次 `clip2table harvest -p 卡.toml -d 这个目录`")
    return p


def iter_csv_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)
