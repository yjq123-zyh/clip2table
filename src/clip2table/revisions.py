"""Immutable captures, parsing snapshots, results and cumulative revisions.

A capture identifies one page observation within one run. A revision selects one
parse for every retained capture; new runs add observations and reparsing changes
only the selected parse. A dataset writer lock protects publication and selection.
Legacy pages are adopted explicitly into a separate dataset.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
import uuid
import threading
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .errors import InputError
from .models import Captured, FieldTrace, ParseResult, Row

# Layout versions are independent of the card schema and of the API version.
# Bumping one must not force the others to move.
LAYOUT = 2
PARSE_FORMAT = 1

DATASET_JSON = "dataset.json"
CAPTURES = "captures"
RULES = "rules"
PARSES = "parses"
REVISIONS = "revisions"
RUNS = "runs"
ACTIVE = "active.json"
_LOCKS: dict[str, threading.RLock] = {}
_DEPTH = threading.local()


@contextmanager
def writer(root: str | Path):
    """One process/thread owns a dataset's mutation boundary; nested calls reuse it."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = str(root)
    lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_DEPTH, "held", {})
        _DEPTH.held = depths
        if key in depths:
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with (root / ".writer.lock").open("a+b") as fh:
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise InputError("这个数据集已有写操作，请稍后重试", code="dataset_busy") from exc
            depths[key] = 1
            try:
                yield
            finally:
                del depths[key]
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def writing(func):
    @wraps(func)
    def locked(root, *args, **kwargs):
        with writer(root if isinstance(root, (str, Path)) else root.root):
            return func(root, *args, **kwargs)
    return locked

# v1 files, only looked at to decide whether an old directory is old.
LEGACY_STATE = "state.json"
LEGACY_ROWS = "rows.csv"
LEGACY_PAGES = "pages"


def _now() -> str:
    """Local timestamp with microseconds.

    Seconds were not enough: a two-page collection writes both pages inside one
    second, and two observations of the same order then carry the same
    `observed_at`. Anything that later asks "which of these did I see first" gets
    an answer that depends on file ordering -- so the clock has to be finer than
    the operation, not finer than a human.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dump(obj: Any, path: Path) -> None:
    """Write JSON atomically. Half a file is worse than no file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"数据文件无法读取：{path}", code="corrupt_metadata") from exc


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")           # separator, so ("ab","c") != ("a","bc")
    return h.hexdigest()


def parser_build() -> str:
    """Which code produced a parse. A fix must not look like the same parse."""
    return f"{__version__}+p{PARSE_FORMAT}"


# --------------------------------------------------------------- layout


def layout_of(path: str | Path) -> int:
    """0 = nothing there, 1 = the old pages/rows.csv layout, 2 = this one."""
    p = Path(path)
    if not p.is_dir():
        return 0
    if (p / DATASET_JSON).is_file():
        return 2
    if (p / LEGACY_STATE).is_file() or (p / LEGACY_ROWS).is_file():
        return 1
    if (p / LEGACY_PAGES).is_dir():
        return 1
    return 0


@writing
def init(path: str | Path, *, dataset_id: str = "") -> dict[str, Any]:
    """Create `dataset.json` if absent. Never creates captures/ or parses/.

    Creating empty directories up front would make `layout_of()` report 2 for a
    dataset that has no content yet, which is indistinguishable from one that
    was upgraded and is empty -- two states that deserve different advice.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    meta_path = p / DATASET_JSON
    meta = _load(meta_path, None)
    if isinstance(meta, dict):
        if meta.get("layout") != LAYOUT:
            raise InputError(
                f"{p} 的数据布局是 {meta.get('layout')!r}，这个版本只认 {LAYOUT}",
                hint="新版本只增不改旧文件；遇到更高版本请升级 clip2table")
        return meta
    meta = {"layout": LAYOUT,
            "dataset_id": dataset_id or "ds-" + _sha(str(p.resolve()))[:12],
            "created": _now(),
            "format": {"layout": LAYOUT, "parse": PARSE_FORMAT}}
    _dump(meta, meta_path)
    return meta


def dataset_meta(path: str | Path) -> dict[str, Any]:
    meta = _load(Path(path) / DATASET_JSON, {})
    return meta if isinstance(meta, dict) else {}


# --------------------------------------------------------------- captures


@dataclass
class CaptureRef:
    """One observation of one page. Immutable once written."""

    capture_id: str
    run_id: str
    page: int
    kind: str
    payload_hash: str
    source_file: str
    observed_at: str = ""
    requested_url: str = ""
    observed_url: str = ""
    channel: str = ""
    engine: str = ""
    formats: list[str] = field(default_factory=list)
    chars: int = 0
    raw_available: bool = True
    legacy_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CaptureRef":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def _cap_dir(root: Path, capture_id: str) -> Path:
    return root / CAPTURES / capture_id


@writing
def write_capture(root: str | Path, cap: Captured, *,
                  run_id: str, page: int, observed_at: str | None = None) -> CaptureRef:
    """Persist the payload exactly as the channel handed it over.

    `kind`, `formats` and the JSON payload are stored because they are what a
    later parse needs and what v1 threw away. Without them "re-parse this page"
    is a question the data cannot answer.
    """
    root = Path(root)
    text = cap.payload if isinstance(cap.payload, str) else ""
    if cap.kind == "json":
        text = json.dumps(cap.json_payload, ensure_ascii=False, sort_keys=True)
    payload_hash = "sha256:" + _sha(text)
    previous = next((r for r in list_captures(root) if r.run_id == run_id and r.page == page), None)
    if previous:
        if previous.payload_hash != payload_hash or previous.kind != cap.kind:
            raise InputError("同一运行的这个页面已经保存了不同内容", code="observation_conflict")
        return previous
    # Observation identity: the run, the page, and the bytes. Same bytes in a
    # new run is a new capture on purpose -- see the module docstring.
    capture_id = "c-" + _sha(run_id, str(page))[:16]

    d = _cap_dir(root, capture_id)
    suffix = {"text": "txt", "html": "html", "json": "json"}.get(cap.kind, "txt")
    source = d / f"source.{suffix}"
    ref = CaptureRef(
        capture_id=capture_id, run_id=run_id, page=page, kind=cap.kind,
        payload_hash=payload_hash, source_file=source.name,
        observed_at=_now() if observed_at is None else observed_at,
        requested_url=cap.requested_url or "", observed_url=cap.observed_url or "",
        channel=cap.channel or "", engine=cap.engine or "",
        formats=list(cap.formats or []), chars=len(text))

    if (d / "meta.json").is_file():
        # Already written. Re-running the same page of the same run must not
        # rewrite an immutable object, and must not fail either.
        return CaptureRef.from_dict(_load(d / "meta.json", ref.as_dict()))

    stage = d.with_name(".tmp-" + d.name + "-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    (stage / source.name).write_bytes(text.encode("utf-8"))
    _dump(ref.as_dict(), stage / "meta.json")
    if d.exists():
        import shutil
        shutil.rmtree(d)
    stage.replace(d)
    return ref


def read_capture(root: str | Path,
                 capture_id: str) -> tuple[CaptureRef, Captured]:
    """The reference and the payload, rebuilt as the channel had it."""
    d = _cap_dir(Path(root), capture_id)
    meta = _load(d / "meta.json", None)
    if not isinstance(meta, dict):
        raise InputError(f"没有这份捕获：{capture_id}",
                         hint=f"看已有的：{Path(root) / CAPTURES}")
    ref = CaptureRef.from_dict(meta)
    if not ref.raw_available:
        raise InputError(f"捕获 {capture_id} 没有可重算的原文", code="raw_unavailable")
    src = d / ref.source_file
    if not src.is_file():
        raise InputError(f"捕获 {capture_id} 的原文不见了：{src}")
    text = src.read_bytes().decode("utf-8")
    if "sha256:" + _sha(text) != ref.payload_hash:
        raise InputError(f"捕获 {capture_id} 的原文摘要不一致", code="corrupt_capture")
    if ref.kind == "json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise InputError(f"捕获 {capture_id} 的 JSON 原文坏了：{e}") from None
        cap = Captured(kind="json", json_payload=obj, channel=ref.channel,
                       engine=ref.engine, formats=list(ref.formats))
    else:
        cap = Captured(kind=ref.kind, payload=text, channel=ref.channel,
                       engine=ref.engine, formats=list(ref.formats))
    cap.requested_url = ref.requested_url
    cap.observed_url = ref.observed_url
    return ref, cap


def list_captures(root: str | Path) -> list[CaptureRef]:
    out: list[CaptureRef] = []
    base = Path(root) / CAPTURES
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if d.name.startswith("."):
            continue
        meta = _load(d / "meta.json", None)
        if isinstance(meta, dict):
            out.append(CaptureRef.from_dict(meta))
    return out


# ------------------------------------------------------------ rule snapshot


@dataclass
class RuleSnapshot:
    """Everything the parser needs, so a rule can be reloaded without the card file."""

    rule_id: str
    card_name: str
    card_schema: int
    created: str = ""
    source_path: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleSnapshot":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def card_config(card) -> dict[str, Any]:
    """Complete execution snapshot, independent from the parsing identity."""
    return dataclasses.asdict(card)


def restore_card(config: dict[str, Any]):
    from .cards.schema import Card
    from .pipeline.extract import FieldSpec
    body = dict(config)
    body["fields"] = [FieldSpec(**f) for f in body.get("fields", [])]
    return Card(**body)


def parse_config(card) -> dict[str, Any]:
    from .dataset import _card_fingerprint
    full = _card_fingerprint(card)
    return {"fingerprint_version": 2, "segment": full["segment"],
            "fields": full["fields"], "dedupe": {"keys": list(card.dedupe_key)}}


@writing
def write_rule(root: str | Path, card) -> RuleSnapshot:
    """Freeze what a card MEANS, so the same rules can be replayed later.

    The snapshot is the card's fingerprint (see `dataset._card_fingerprint`),
    not a copy of the TOML: comments and layout are not parse semantics, and
    including them would give two identical rules two ids.
    """
    from .dataset import _card_fingerprint

    config = parse_config(card)
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    rule_id = "r2-" + _sha(payload)[:16]
    snap = RuleSnapshot(rule_id=rule_id, card_name=getattr(card, "name", ""),
                        card_schema=int(getattr(card, "schema", 1) or 1),
                        created=_now(), source_path=str(getattr(card, "path", "")),
                        config=config)
    path = Path(root) / RULES / f"{rule_id}.json"
    if not path.is_file():
        _dump(snap.as_dict(), path)
    return snap


def load_rule(root: str | Path, rule_id: str) -> RuleSnapshot:
    d = _load(Path(root) / RULES / f"{rule_id}.json", None)
    if not isinstance(d, dict):
        raise InputError(f"没有这份规则快照：{rule_id}")
    if d.get("config", {}).get("fingerprint_version", 1) not in (1, 2):
        raise InputError("不支持这个规则指纹版本", code="unsupported_version")
    return RuleSnapshot.from_dict(d)


def card_from_rule(snap: RuleSnapshot):
    """Reload a snapshot as a Card the parser will accept.

    This is the check that makes a snapshot worth storing: if it cannot be
    turned back into a card and validated by `parse_card`, then "same rules
    later" was never true.
    """
    from .cards.emit import toml_key, toml_str
    from .cards.schema import parse_card

    c = snap.config or {}
    seg = c.get("segment") or {}
    dedupe = c.get("dedupe") or {}
    data: dict[str, Any] = {
        "schema": int(snap.card_schema or 1),
        "name": c.get("name") or snap.card_name,
        "segment": {"mode": seg.get("mode") or "anchor"},
        "fields": {},
    }
    if c.get("channel"):
        data["channel"] = c["channel"]
    for key in ("anchor", "anchor_next"):
        if seg.get(key):
            data["segment"][key] = seg[key]
    if seg.get("min_lines") is not None:
        data["segment"]["min_lines"] = seg["min_lines"]
    if seg.get("skip_header"):
        data["segment"]["skip_header"] = True
    if seg.get("record_lines"):
        data["segment"]["record_lines"] = list(seg["record_lines"])
    if seg.get("required"):
        data["segment"]["required"] = list(seg["required"])
    if seg.get("mode") == "periodic":
        data["segment"]["period"] = seg.get("period")
        data["segment"]["offset"] = int(seg.get("offset") or 0)
        if seg.get("count") is not None:
            data["segment"]["count"] = seg["count"]

    for f in c.get("fields") or []:
        if not isinstance(f, dict):
            continue
        body = dict(f.get("def") or {})
        body.pop("name", None)
        if f.get("type") and "type" not in body:
            body["type"] = f["type"]
        data["fields"][f.get("name") or ""] = body
    if dedupe.get("keys"):
        data["dedupe"] = {"key": list(dedupe["keys"]),
                          "on_collision": dedupe.get("on_collision") or "report_only"}
        if dedupe.get("identity"):
            data["dedupe"]["identity"] = list(dedupe["identity"])
        if dedupe.get("mutable"):
            data["dedupe"]["mutable"] = list(dedupe["mutable"])
    paging = c.get("paging") or {}
    if paging.get("style") and paging["style"] != "none":
        data["paging"] = {k: v for k, v in paging.items() if v is not None}
    if c.get("fetch"):
        data["fetch"] = dict(c["fetch"])
    if c.get("target"):
        data["target"] = dict(c["target"])
    if c.get("harvest"):
        data["harvest"] = dict(c["harvest"])
    # `[ledger]` is part of the rule: it says which observation of an entity is
    # the current one. A snapshot that drops it reloads with the default
    # `observed` ordering, so a revision read through its own snapshot could
    # show a different "current" row than the same revision read through the
    # card -- the exact divergence `api.ledger_view` exists to prevent.
    if c.get("ledger"):
        data["ledger"] = dict(c["ledger"])
    return parse_card(data, f"规则快照 {snap.rule_id}（{snap.card_name}）")


# ------------------------------------------------------------ parse result


@dataclass
class ParseRef:
    """One capture, parsed by one rule, with one parser build."""

    parse_id: str
    capture_id: str
    rule_id: str
    parser_build: str
    result_schema: int
    created: str = ""
    rows: int = 0
    unparsed: int = 0
    total_lines: int = 0
    consumed_lines: int = 0
    level: str = ""
    fill: dict[str, float] = field(default_factory=dict)
    legacy_policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParseRef":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def complete(self) -> bool:
        """A parse whose lines balance can be used as evidence."""
        accounted = self.consumed_lines + self.unparsed
        return self.total_lines == 0 or accounted == self.total_lines


def _row_record(row: Row) -> dict[str, Any]:
    return {"values": row.values,
            "source_page": row.source_page,
            "source_line": row.source_line,
            # Kept because a later parse is compared with this one at line
            # granularity, and "this record starts one line later now" is the
            # boundary change a value diff cannot see.
            "raw_at": list(row.raw_at),
            "raw_lines": list(row.raw_lines),
            "traces": {n: t.as_dict() for n, t in row.traces.items()}}


def _row_from(rec: dict[str, Any]) -> Row:
    traces = {}
    for name, d in (rec.get("traces") or {}).items():
        if isinstance(d, dict):
            try:
                traces[str(name)] = FieldTrace.from_dict(str(name), d)
            except TypeError:
                continue
    return Row(values=dict(rec.get("values") or {}),
               raw_lines=list(rec.get("raw_lines") or []),
               source_page=rec.get("source_page"),
               source_line=rec.get("source_line"),
               raw_at=list(rec.get("raw_at") or []),
               traces=traces)


@writing
def write_parse(root: str | Path, cap_ref: CaptureRef, rule: RuleSnapshot,
                res: ParseResult, *, legacy_policy=None) -> ParseRef:
    """Publish one parse. Immutable: a new rule or build makes a new one."""
    root = Path(root)
    build = "unknown" if res.level == "legacy" else parser_build()
    parse_id = "p-" + _sha(cap_ref.capture_id, rule.rule_id,
                           build, str(PARSE_FORMAT))[:16]
    d = root / PARSES / parse_id
    ref = ParseRef(
        parse_id=parse_id, capture_id=cap_ref.capture_id,
        rule_id=rule.rule_id, parser_build=build,
        result_schema=PARSE_FORMAT, created=_now(),
        rows=len(res.rows), unparsed=len(res.unparsed),
        total_lines=res.total_lines, consumed_lines=res.consumed_lines,
        level=res.level, fill=res.fill_rates, legacy_policy=legacy_policy or {})
    if (d / "meta.json").is_file():
        return ParseRef.from_dict(_load(d / "meta.json", ref.as_dict()))

    stage = d.with_name(".tmp-" + d.name + "-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    with (stage / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row in res.rows:
            fh.write(json.dumps(_row_record(row), ensure_ascii=False) + "\n")
    (stage / "unparsed.txt").write_text("\n".join(res.unparsed), encoding="utf-8")
    meta = ref.as_dict()
    meta["notes"] = list(res.notes)
    meta["deviations"] = list(res.deviations)
    # Column order, kept so a reload returns the same table rather than a
    # version sorted by key.
    meta["fields"] = list(res.fields)
    _dump(meta, stage / "meta.json")
    if d.exists():
        import shutil
        shutil.rmtree(d)
    stage.replace(d)
    return ref


def read_parse(root: str | Path,
               parse_id: str) -> tuple[ParseRef, ParseResult]:
    d = Path(root) / PARSES / parse_id
    meta = _load(d / "meta.json", None)
    if not isinstance(meta, dict):
        raise InputError(f"没有这份解析结果：{parse_id}")
    ref = ParseRef.from_dict(meta)
    if ref.result_schema != PARSE_FORMAT:
        raise InputError(f"不支持解析结果格式 {ref.result_schema}", code="unsupported_version")
    rows: list[Row] = []
    p = d / "rows.jsonl"
    if not p.is_file() or not (d / "unparsed.txt").is_file():
        raise InputError(f"解析结果 {parse_id} 的文件不完整", code="corrupt_parse")
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(_row_from(json.loads(line)))
    if len(rows) != ref.rows:
        raise InputError(f"解析结果 {parse_id} 的行数与元数据不符", code="corrupt_parse")
    up = d / "unparsed.txt"
    unparsed = (up.read_text(encoding="utf-8").splitlines()
                if up.is_file() and up.stat().st_size else [])
    res = ParseResult(rows=rows,
                      # Stored, not recomputed: column order is part of what a
                      # parse means, and deriving it from a set would silently
                      # sort 订单号/客户/金额 into alphabetical order on reload.
                      fields=list(meta.get("fields") or []),
                      unparsed=unparsed, total_lines=ref.total_lines,
                      consumed_lines=ref.consumed_lines, level=ref.level,
                      notes=list(meta.get("notes") or []),
                      deviations=list(meta.get("deviations") or []))
    return ref, res


def list_parses(root: str | Path) -> list[ParseRef]:
    out: list[ParseRef] = []
    base = Path(root) / PARSES
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if d.name.startswith("."):
            continue
        meta = _load(d / "meta.json", None)
        if isinstance(meta, dict):
            out.append(ParseRef.from_dict(meta))
    return out


# ------------------------------------------------------- result revision


@dataclass
class ResultRevision:
    """An ordered map capture -> parse, plus the revision it descends from."""

    revision_id: str
    created: str = ""
    reason: str = ""
    parent: str | None = None
    entries: list[dict[str, str]] = field(default_factory=list)
    complete: bool = True
    note: str = ""
    references_complete: bool = True
    coverage_complete: bool = True
    operation_complete: bool = True
    quality_complete: bool = True
    failed: list[dict[str, str]] = field(default_factory=list)
    requested_captures: list[str] = field(default_factory=list)
    target_rule: str = ""
    policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResultRevision":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def parses(self) -> list[str]:
        return [e["parse_id"] for e in self.entries if e.get("parse_id")]

    @property
    def captures(self) -> list[str]:
        return [e["capture_id"] for e in self.entries if e.get("capture_id")]


def _page_of(root: Path, capture_id: str) -> int | None:
    cap = _load(root / CAPTURES / capture_id / "meta.json", {})
    n = cap.get("page") if isinstance(cap, dict) else None
    return n if isinstance(n, int) else None


def _ordered(root: Path, entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Entries in page order, so a revision reads like the source did.

    Ordering by capture page (not by insertion) keeps two revisions comparable
    side by side, and keeps a revision's list stable when it is rebuilt from a
    different set of runs.
    """
    def page_of(item: tuple[str, str]) -> int:
        n = _page_of(root, item[0])
        return n if n is not None else 10 ** 6
    return sorted(entries, key=page_of)


def carry_forward(root: str | Path, base_revision: str | None,
                  entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep every observation; replace only the parse for an existing capture."""
    root = Path(root)
    if not base_revision:
        return list(entries)
    base = read_revision(root, base_revision)
    covered = {cid for cid, _ in entries}
    carried = [(e["capture_id"], e["parse_id"]) for e in base.entries
               if e.get("parse_id")
               and e.get("capture_id") not in covered]
    return carried + list(entries)


@writing
def write_revision(root: str | Path, entries: list[tuple[str, str]], *,
                   parent: str | None = None, reason: str = "",
                   note: str = "", failed=None, requested_captures=None,
                   target_rule: str = "", operation_complete: bool = True,
                   policy=None) -> ResultRevision:
    """Publish a revision. Refuses entries whose capture or parse is missing.

    `complete` is False when any referenced parse does not balance its lines.
    An incomplete candidate is still written -- being able to look at a partial
    reparse is the point of A03 -- but `activate()` will not make it the table.
    """
    root = Path(root)
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    complete = True
    for capture_id, parse_id in entries:
        if capture_id in seen:
            raise InputError(f"结果版本里同一个捕获出现了两次：{capture_id}",
                             hint="一个捕获在一个版本里只能选一份解析结果")
        seen.add(capture_id)
        if not (root / CAPTURES / capture_id / "meta.json").is_file():
            raise InputError(f"结果版本引用了不存在的捕获：{capture_id}")
        pref = _load(root / PARSES / parse_id / "meta.json", None)
        if not isinstance(pref, dict):
            raise InputError(f"结果版本引用了不存在的解析结果：{parse_id}")
        ref = ParseRef.from_dict(pref)
        if ref.capture_id != capture_id:
            raise InputError(
                f"解析结果 {parse_id} 不是捕获 {capture_id} 的（它属于 {ref.capture_id}）",
                hint="同一版本里每个捕获只能配自己那份解析")
        if not ref.complete:
            complete = False
        cap_meta = _load(root / CAPTURES / capture_id / "meta.json", {})
        if cap_meta.get("raw_available", True):
            read_capture(root, capture_id)
        read_parse(root, parse_id)
        load_rule(root, ref.rule_id)
        rows.append({"capture_id": capture_id, "parse_id": parse_id})

    if parent:
        pref = _load(root / REVISIONS / f"{parent}.json", None)
        if not isinstance(pref, dict):
            raise InputError(f"父版本不存在：{parent}")
    body = json.dumps([[e["capture_id"], e["parse_id"]] for e in rows],
                      ensure_ascii=False, sort_keys=True)
    failed = list(failed or [])
    requested = list(requested_captures) if requested_captures is not None else [r["capture_id"] for r in rows]
    coverage = set(requested) <= seen
    inherited_policy = read_revision(root, parent).policy if parent else {}
    pol = policy if policy is not None else inherited_policy
    identity = json.dumps([failed, requested, target_rule, operation_complete, pol],
                          ensure_ascii=False, sort_keys=True)
    revision_id = "v-" + _sha(parent or "", body, reason, identity)[:16]
    rev = ResultRevision(revision_id=revision_id, created=_now(),
                         reason=reason, parent=parent, entries=rows,
                         complete=complete and coverage and operation_complete and not failed,
                         note=note, quality_complete=complete,
                         coverage_complete=coverage, operation_complete=operation_complete and not failed,
                         failed=failed, requested_captures=requested,
                         target_rule=target_rule, policy=pol)
    path = root / REVISIONS / f"{revision_id}.json"
    if not path.is_file():
        _dump(rev.as_dict(), path)
    return rev


def read_revision(root: str | Path, revision_id: str) -> ResultRevision:
    d = _load(Path(root) / REVISIONS / f"{revision_id}.json", None)
    if not isinstance(d, dict):
        raise InputError(f"没有这个结果版本：{revision_id}")
    return ResultRevision.from_dict(d)


def list_revisions(root: str | Path) -> list[ResultRevision]:
    """Every revision, oldest first.

    Sorted by creation time, not by file name: `revision_id` is a content hash,
    so globbing the directory returns them in an order that has nothing to do
    with the order they were made. Callers read "the last one" as "the newest",
    and a hash order silently answers a different question.
    """
    out: list[ResultRevision] = []
    base = Path(root) / REVISIONS
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*.json")):
        d = _load(p, None)
        if isinstance(d, dict):
            out.append(ResultRevision.from_dict(d))
    out.sort(key=lambda r: (r.created, r.revision_id))
    return out


@writing
def activate(root: str | Path, revision_id: str, *,
             expected: str | None = None, policy: dict[str, Any] | None = None,
             allow_incomplete: bool = False, expected_token=None,
             operation_id: str = "", decision_set_id: str | None = None) -> dict[str, Any]:
    """Point the table at one revision. The only place `active.json` changes.

    `expected` is the revision the caller believes is active. A mismatch means
    someone else moved the pointer while this operation was running, and
    overwriting it would discard a decision that was not ours to discard.
    """
    root = Path(root)
    rev = read_revision(root, revision_id)
    for entry in rev.entries:
        cap_meta = _load(root / CAPTURES / entry["capture_id"] / "meta.json", None)
        if cap_meta is None:
            raise InputError("版本引用的捕获缺失", code="corrupt_capture")
        if cap_meta.get("raw_available", True):
            read_capture(root, entry["capture_id"])
        pref, _ = read_parse(root, entry["parse_id"])
        load_rule(root, pref.rule_id)
    if not rev.operation_complete or not rev.coverage_complete or rev.failed:
        raise InputError("候选的目标范围尚未处理完成", code="incomplete_result")
    if not rev.complete and not allow_incomplete:
        raise InputError(
            f"版本 {revision_id} 不完整（有解析结果的行数不守恒），不能当作台账",
            hint="先修好那几页的规则重算；只想看看的话加 allow_incomplete")
    cur = active(root)
    if expected_token is not None and expected_token != active_token(root):
        raise InputError("当前选择已改变，请重新读取状态", code="revision_conflict")
    now = cur.get("revision_id") or None
    if expected is not None:
        # "" and None are different questions: None means "I am not checking",
        # "" means "I believe nothing is active yet". Callers that read the base
        # from a function returning "" (the first collection has no parent) mean
        # the second, so normalise here instead of at every call site.
        want = expected or None
        if want != now:
            raise InputError(
                f"当前选中的版本是 {now or '（还没有）'}，"
                f"不是你以为的 {want or '（还没有）'}",
                hint="重新读一遍当前状态再决定", code="revision_conflict")
    out = {"revision_id": revision_id, "selected_at": _now(),
           "policy": policy if policy is not None else rev.policy or cur.get("policy"),
           "previous": now, "generation": int(cur.get("generation", 0)) + 1,
           "operation_id": operation_id,
           "decision_set_id": decision_set_id if decision_set_id is not None else cur.get("decision_set_id", "")}
    _dump(out, root / ACTIVE)
    return out


def active(root: str | Path) -> dict[str, Any]:
    d = _load(Path(root) / ACTIVE, {})
    return d if isinstance(d, dict) else {}


def active_token(root: str | Path) -> dict[str, Any]:
    cur = active(root)
    return {"revision_id": cur.get("revision_id", ""), "generation": cur.get("generation", 0)}


# ---------------------------------------------------------------- runs


@dataclass
class RunRecord:
    """One operation. Separate from the captures it produced."""

    run_id: str
    task: str = ""
    kind: str = "collect"      # collect | resume | reparse | export
    created: str = ""
    status: str = "running"    # running | completed | failed | cancelled
    rule_id: str = ""
    base_revision: str | None = None
    captures: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    entries: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@writing
def new_run(root: str | Path, *, task: str, kind: str = "collect",
            rule_id: str = "", base_revision: str | None = None,
            config=None, operation_id: str = "", run_id: str = "") -> RunRecord:
    run = RunRecord(run_id=run_id or "run-" + _sha(str(time.time_ns()))[:14], task=task,
                    kind=kind, created=_now(), rule_id=rule_id,
                    base_revision=base_revision, config=config or {}, operation_id=operation_id)
    _save_run(root, run)
    return run


@writing
def save_run(root: str | Path, run: RunRecord) -> RunRecord:
    _save_run(root, run)
    return run


def _save_run(root: str | Path, run: RunRecord) -> None:
    _dump(run.as_dict(), Path(root) / RUNS / f"{run.run_id}.json")


def read_run(root: str | Path, run_id: str) -> RunRecord:
    d = _load(Path(root) / RUNS / f"{run_id}.json", None)
    if not isinstance(d, dict):
        raise InputError(f"没有这次运行：{run_id}")
    return RunRecord.from_dict(d)


def list_runs(root: str | Path) -> list[RunRecord]:
    out: list[RunRecord] = []
    base = Path(root) / RUNS
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*.json")):
        d = _load(p, None)
        if isinstance(d, dict):
            out.append(RunRecord.from_dict(d))
    return out


# ------------------------------------------------------------- publishing


class Publisher:
    """Turns pages into captures, parses and one revision, in that order.

    This is the boundary `flow.Committer` sits next to. The committer owns
    `pages/` (v1 layout, unchanged, still what `task run` writes); this owns the
    v2 objects. Two writers on two different sets of files, so neither has to
    learn the other's format.

    A page that fails to publish a parse still counts as a capture. Losing the
    text because the parse went wrong would be losing the evidence along with
    the conclusion -- and the text is the part that cannot be re-obtained once
    the page has changed.
    """

    def __init__(self, root: str | Path, card, *, run: RunRecord,
                 base_revision: str | None = None):
        self.root = Path(root)
        self.card = card
        self.run = run
        self.base_revision = base_revision
        self.rule = write_rule(self.root, card)
        self.entries = [(e["capture_id"], e["parse_id"]) for e in run.entries]
        # One rule per run: the card cannot change mid-run without the whole
        # revision meaning two different things.
        self.run.rule_id = self.rule.rule_id
        if not self.run.config:
            self.run.config = {"card": card_config(card)}
        save_run(self.root, self.run)

    @writing
    def page(self, page: int, cap: Captured,
             res: ParseResult | None) -> tuple[CaptureRef, ParseRef | None]:
        cref = write_capture(self.root, cap, run_id=self.run.run_id, page=page)
        if cref.capture_id not in self.run.captures:
            self.run.captures.append(cref.capture_id)
        pref: ParseRef | None = None
        if res is not None:
            pref = write_parse(self.root, cref, self.rule, res)
            if all(c != cref.capture_id for c, _ in self.entries):
                self.entries.append((cref.capture_id, pref.parse_id))
        self.run.checkpoint = {"pages": len(self.run.captures),
                               "parses": len(self.entries), "next_page": page + 1}
        self.run.entries = [{"capture_id": c, "parse_id": p} for c, p in self.entries]
        save_run(self.root, self.run)
        return cref, pref

    @writing
    def publish(self, *, reason: str = "", activate_now: bool = True,
                note: str = "") -> ResultRevision | None:
        """Close the run with one revision. No pages, no revision.

        `base_revision`'s captures are carried forward (see `carry_forward`), so
        what gets published is the ledger as of now, not the pages this run
        happened to touch.
        """
        self.run.status = "completed" if self.entries else "failed"
        if not self.entries:
            self.run.note = self.run.note or "没有成功解析任何一页"
            save_run(self.root, self.run)
            return None
        entries = carry_forward(self.root, self.base_revision, self.entries)
        failed = [{"capture_id": c, "why": "原文已保存，尚未成功解析"}
                  for c in self.run.captures if c not in {cid for cid, _ in self.entries}]
        rev = write_revision(self.root, entries,
                             parent=self.base_revision,
                             reason=reason or f"{self.run.kind}: {self.card.name}",
                             note=note, policy=self._policy(), failed=failed,
                             operation_complete=not failed)
        self.run.status = "completed"
        save_run(self.root, self.run)
        if activate_now:
            # An incomplete candidate is published but never becomes the table:
            # a half-recalculated ledger that looks complete is worse than one
            # that refuses to switch.
            if rev.complete:
                activate(self.root, rev.revision_id, expected=self.base_revision or "",
                         allow_incomplete=False)
            else:
                self.run.note = ("版本不完整，没有切换台账"
                                 if not self.run.note else self.run.note)
                save_run(self.root, self.run)
        return rev

    def _policy(self):
        from .ledger import policy_from_card
        return policy_from_card(self.card).as_dict()


# ---------------------------------------------------------- legacy pages


def legacy_pages(path: str | Path) -> list[dict[str, Any]]:
    """What a v1 dataset can and cannot hand to the new layout, per page.

    Reported rather than decided: a page with no `kind` may still be perfectly
    re-parseable, but only the user knows whether they copied text or HTML.
    Guessing here would produce a parse that is either empty or wrong, and both
    look like data.

    Pages that already have a capture in this same directory are skipped. A new
    dataset keeps writing `pages/` (that is the table `Dataset` maintains), so
    without this filter every page written by this very version would be listed
    as old data that needs migrating.
    """
    p = Path(path)
    out: list[dict[str, Any]] = []
    base = p / LEGACY_PAGES
    if not base.is_dir():
        return out
    migrated = {c.page for c in list_captures(p)}
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith(".tmp"):
            continue
        try:
            page = int(d.name)
        except ValueError:
            continue
        if page in migrated:
            continue
        meta = _load(d / "meta.json", {}) or {}
        has_raw = (d / "raw.txt").is_file() and bool(meta.get("raw_available", True))
        out.append({
            "page": page,
            "has_raw": has_raw,
            # v1 never stored kind; only the presence of CF_HTML in what the
            # channel reported can hint at it, and even that is not recorded
            # per page in older datasets.
            "kind": meta.get("kind") or "",
            "kind_known": bool(meta.get("kind")),
            "has_rule_snapshot": bool(meta.get("card_snapshot")),
            "has_field_specs": bool(meta.get("field_specs")),
            "rows": meta.get("row_count"),
            "reparseable": bool(has_raw and meta.get("kind")),
            "why_not": ("" if has_raw and meta.get("kind")
                        else "原文缺失" if not has_raw
                        else "不知道这一页是文本还是 HTML"),
        })
    return out


@writing
def adopt_legacy_page(root: str | Path, ds_path: str | Path,
                      page: int, *, assume_kind: str = ""
                      ) -> tuple[CaptureRef, ParseRef]:
    """Register one v1 page as a capture plus a compatible parse.

    The parse rows come from the page's own `rows.json`, not from the merged
    `rows.csv`: the CSV has already had the collision policy applied, so the
    records that lost are gone from it. Rebuilding a "historical" parse from
    the filtered view would invent a history in which those records never
    existed.
    """
    root, ds_path = Path(root), Path(ds_path)
    d = ds_path / LEGACY_PAGES / f"{page:04d}"
    meta = _load(d / "meta.json", None)
    if not isinstance(meta, dict):
        raise InputError(f"旧数据集里没有第 {page} 页")
    has_raw = (d / "raw.txt").is_file() and meta.get("raw_available", True)
    kind = meta.get("kind") or assume_kind
    if has_raw and kind not in ("text", "html", "json"):
        raise InputError(
            f"第 {page} 页不知道是文本还是 HTML，不能替你选解析器",
            hint="这一页当时没有记录捕获类型；用 assume_kind 明确声明"
                 "（文字/表格复制是 text，网页源码是 html），"
                 "或者先用这条规则单独解析同一份原文验证结果对得上",
            reason="legacy_capture_type_required", code="capture_type_required")

    text = (d / "raw.txt").read_text(encoding="utf-8") if has_raw else ""
    # Page numbers belong to the run they were fetched in. Writing the old page
    # number here would make an adopted capture collide with a new run's page 1.
    legacy_id = "legacy-" + _sha(str(ds_path.resolve()))[:20]
    if has_raw:
        cap = Captured(kind=kind, payload=text,
                       requested_url=str(meta.get("requested_url") or ""),
                       observed_url=str(meta.get("observed_url") or ""),
                       channel=str(meta.get("channel") or ""),
                       engine=str(meta.get("engine") or ""),
                       formats=list(meta.get("formats") or []))
        if kind == "json":
            cap.json_payload = json.loads(text)
        cap_ref = write_capture(root, cap, run_id=legacy_id, page=page,
                                observed_at=str(meta.get("observed_at") or ""))
    else:
        cid = "c-" + _sha(legacy_id, str(page))[:16]
        cap_ref = CaptureRef(capture_id=cid, run_id=legacy_id, page=page,
                             kind="unknown", payload_hash="", source_file="",
                             raw_available=False, legacy_source=str(d.resolve()),
                             observed_at=str(meta.get("observed_at") or ""))
        _dump(cap_ref.as_dict(), root / CAPTURES / cid / "meta.json")

    from .cards.schema import Card
    from .pipeline.extract import FieldSpec

    specs = []
    for s in meta.get("field_specs") or []:
        body = dict(s.get("def") or {})
        if body:
            body.setdefault("name", s.get("name"))
            try:
                specs.append(FieldSpec(**body))
            except TypeError:
                continue
    if not specs:
        specs = [FieldSpec(name=n, kind="line", index=i)
                 for i, n in enumerate(meta.get("fields") or [])]
    snap = meta.get("card_snapshot") or {}
    seg = (snap.get("segment") or {}) if isinstance(snap, dict) else {}
    dedupe = (snap.get("dedupe") or {}) if isinstance(snap, dict) else {}
    card = Card(
        name=str(snap.get("name") or meta.get("card") or "legacy"),
        channel=snap.get("channel") or None,
        fields=specs,
        segment_mode=seg.get("mode") or "lines",
        anchor=seg.get("anchor"),
        anchor_next=seg.get("anchor_next"),
        min_lines=int(seg.get("min_lines") or 1),
        skip_header=bool(seg.get("skip_header")),
        record_lines=tuple(seg["record_lines"]) if seg.get("record_lines") else None,
        required=list(seg.get("required") or []),
        segment_period=seg.get("period"),
        segment_offset=int(seg.get("offset") or 0),
        segment_count=seg.get("count"),
        dedupe_key=list(dedupe.get("keys") or meta.get("keys") or []),
        on_collision=str(dedupe.get("on_collision")
                         or meta.get("on_collision") or "report_only"),
        identity_fields=list(dedupe.get("identity") or []),
        mutable_fields=list(dedupe.get("mutable") or []),
        ledger=dict(snap.get("ledger") or {}),
    )
    rule = write_rule(root, card)

    # Reuse the rows the page already has. They are the evidence for what this
    # page said at the time; re-parsing would substitute today's rules for the
    # ones in force then, which is the exact substitution this layer exists to
    # avoid. `total_lines` stays 0 because v1 never recorded it -- claiming a
    # conservation check here would be inventing the denominator.
    rows = [_row_from_legacy(r) for r in (_load(d / "rows.json", []) or [])]
    res = ParseResult(rows=rows, fields=list(meta.get("fields") or []),
                      unparsed=[], total_lines=0,
                      consumed_lines=0, level="legacy",
                      notes=["从 v1 页面登记而来：原文位置、未识别行没有留存，"
                             "精确回放只在这一页重新解析之后可用"])
    from .ledger import policy_from_card
    parse_ref = write_parse(root, cap_ref, rule, res, legacy_policy=policy_from_card(card).as_dict())
    return cap_ref, parse_ref


# v1 wrote one flat dict per row: the field values at the top level, plus
# `_page` and `_traces`. Recognising that shape here (and only here) keeps the
# new on-disk format single and unambiguous -- no reader has to ask "is this a
# new record or an old one", because old records are converted on the way in.
_LEGACY_RESERVED = ("_page", "_provenance", "_traces")


def _row_from_legacy(rec: Any) -> Row:
    if not isinstance(rec, dict):
        return Row(values={}, raw_lines=[])
    if "values" in rec and isinstance(rec["values"], dict):
        return _row_from(rec)          # already the new shape
    values = {k: v for k, v in rec.items() if k not in _LEGACY_RESERVED}
    traces = {}
    for name, t in (rec.get("_traces") or {}).items():
        if isinstance(t, dict):
            try:
                traces[str(name)] = FieldTrace.from_dict(str(name), t)
            except TypeError:
                continue
    page = rec.get("_page")
    return Row(values=values, raw_lines=[],
               source_page=page if isinstance(page, int) else None,
               source_line=None, raw_at=[], traces=traces)
