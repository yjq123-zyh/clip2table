"""extract: pull field values out of one record's lines.

Every field uses one of a closed set of primitives. There is no eighth
primitive, and nothing here invents a value: a field the source does not
contain stays None.

  dom    - rendered-DOM selector (browser channel only)
  cell   - a table column (HTML table / TSV). Column edges are facts.
  line   - the nth line of the record (negative counts from the end)
  search - the line matching a pattern, then a capture group
  span   - the text between two markers, possibly across lines
  json   - a dotted path into a parsed JSON document
  const  - a fixed value, for provenance labels only, never to fill data
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from ..models import FieldTrace, Row

KINDS = ("dom", "cell", "line", "search", "span", "json", "const")
TYPES = ("text", "date", "int", "float", "str", "bool")


@dataclass
class FieldSpec:
    name: str
    kind: str = "search"
    type: str = "text"

    # cell
    col: int | None = None

    # line
    index: int = 0

    # search
    pattern: str | None = None
    scope: str = "record"        # record | head | tail
    window: int | None = None    # how many lines scope covers (head/tail)

    # span
    start: str | None = None     # "after:<field>" | "line:<n>" | "after:<literal>"
    end: str | None = None       # "before:<regex>" | "before:<field>"

    # dom
    sel: str | None = None
    attr: str = "text"

    # json
    path: str | None = None

    # const
    value: str | None = None

    # common
    exclude: list[str] = field(default_factory=list)
    min_len: int = 0
    strip: bool = True

    # type conversion (only meaningful for type=int/float). Declared rather than
    # inferred: "1.5万" is a number only if the card says 万 is a unit here, and
    # "10–20" is a range that has no single value unless the card says which end
    # it wants. Guessing either one produces a number the page never stated.
    unit: str | None = None        # literal suffix stripped before parsing
    multiplier: float | None = None  # applied after parsing (unit="万", 10000)
    percent: bool = False          # allow a trailing % / ％; value stays as written
    on_range: str = "reject"       # reject | first | last

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未知的取数原语 kind={self.kind!r}（字段 {self.name}）")
        if self.type not in TYPES:
            raise ValueError(f"未知的字段类型 type={self.type!r}（字段 {self.name}）")
        if self.kind == "search" and not self.pattern:
            raise ValueError(f"字段 {self.name} 用 search 但没给 pattern")
        if self.kind == "span" and not self.start and not self.end:
            raise ValueError(f"字段 {self.name} 用 span 但 from/to 都没给")
        if self.kind == "span":
            for key, expr in (("from", self.start), ("to", self.end)):
                if expr is None:
                    continue
                head = expr.partition(":")[0].strip().rstrip("?")
                if head not in ("line", "after", "before"):
                    raise ValueError(
                        f"字段 {self.name} 的 {key}={expr!r} 不认识："
                        f"只能用 line:/after:/before:，末尾加 ? 表示找不到时不算错")
        if self.kind == "const" and self.value is None:
            raise ValueError(f"字段 {self.name} 用 const 但没给 value")
        if self.scope not in ("record", "head", "tail"):
            raise ValueError(f"字段 {self.name} 的 scope={self.scope!r} 不认识")
        if self.on_range not in ("reject", "first", "last"):
            raise ValueError(
                f"字段 {self.name} 的 on_range={self.on_range!r} 不认识："
                f"只能是 reject/first/last")

    def describe_rule(self) -> str:
        """The rule as written, so a report can say WHICH rule produced a value.

        "客户名 82%" tells the user a field is thin; "客户名 82%（line 3）" tells
        them which line of the card to open. Without the second half they still
        have to read the whole page again.
        """
        where = {"record": "记录内", "head": f"前 {self.window or '?'} 行",
                 "tail": f"后 {self.window or '?'} 行"}.get(self.scope, self.scope)
        if self.kind == "line":
            base = f"line index={self.index}"
        elif self.kind == "cell":
            base = f"cell col={self.col or 0}"
        elif self.kind == "search":
            base = f"search {self.pattern!r}（{where}）"
        elif self.kind == "span":
            base = f"span {self.start or '记录开头'} → {self.end or '记录结尾'}"
        elif self.kind == "const":
            base = f"const {self.value!r}"
        else:
            base = self.kind
        extra = []
        if self.pattern and self.kind != "search":
            extra.append(f"pattern={self.pattern!r}")
        if self.type and self.type != "text":
            extra.append(f"type={self.type}")
        if self.unit:
            extra.append(f"unit={self.unit!r}")
        if self.multiplier is not None:
            extra.append(f"multiplier={self.multiplier}")
        if self.percent:
            extra.append("percent")
        if self.on_range != "reject":
            extra.append(f"on_range={self.on_range}")
        if self.min_len:
            extra.append(f"min_len={self.min_len}")
        if self.exclude:
            extra.append(f"exclude={self.exclude}")
        return base + (f" [{' '.join(extra)}]" if extra else "")

    def rule_hash(self) -> str:
        """Short stable fingerprint of this field's rule for the dataset header."""
        import hashlib
        return hashlib.sha256(self.describe_rule().encode("utf-8")).hexdigest()[:12]


class _Hit(NamedTuple):
    """Raw value plus the reason it is empty when it is (see FieldTrace)."""

    raw: Any = None
    status: str = "ok"
    line: int | None = None
    detail: str = ""


def extract(record: list[str], specs: list[FieldSpec]) -> Row:
    """Extract one record. Fields are resolved in declaration order, because
    span boundaries may reference an earlier field's value.

    Every field gets a trace, including the good ones: the report needs to say
    "empty because no match" for some rows and "empty because the record is
    shorter than the rule expects" for others. Collapsing those into one empty
    cell is what made a broken rule indistinguishable from an absent value.
    """
    values: dict[str, Any] = {}
    traces: dict[str, FieldTrace] = {}
    for spec in specs:
        rule = spec.describe_rule()
        try:
            hit = _one(record, spec, values)
        except (ValueError, re.error) as exc:
            hit = _Hit(status="rule-error", detail=str(exc))
        if hit.raw is None:
            values[spec.name] = None
            traces[spec.name] = FieldTrace(spec.name, spec.kind,
                                           status=hit.status, line=hit.line,
                                           detail=hit.detail, rule=rule)
            continue
        val = _coerce(hit.raw, spec)
        if val is None:
            values[spec.name] = None
            # Text was there; the conversion is what failed. Saying "missing"
            # here would send the user to fix the source instead of the type.
            traces[spec.name] = FieldTrace(spec.name, spec.kind,
                                           status="coerce-failed",
                                           raw=str(hit.raw), line=hit.line,
                                           detail=spec.type, rule=rule)
            continue
        values[spec.name] = val
        traces[spec.name] = FieldTrace(
            spec.name, spec.kind, status="ok", raw=str(hit.raw), line=hit.line,
            rule=rule,
            # A date keeps its原文 in `values`; the normalized form is the extra
            # fact, and only when it is unambiguous. Normalizing in place would
            # rewrite "2026年3月5日" into something the page never said.
            normalized=_iso_date(str(hit.raw)) if spec.type == "date" else None)
    return Row(values=values, raw_lines=list(record), traces=traces)


def _candidates(record: list[str], spec: FieldSpec) -> tuple[int, list[str]]:
    """The lines a search field may match, plus the index of the first one.

    The offset matters: a `tail` field only looks at the last few lines, but the
    trace must still report the line number the page actually had. Returning a
    bare slice made every tail hit claim to be on line 0..window-1, which reads
    as "the value came from the top of the record" -- the opposite of what the
    rule asked for.
    """
    if spec.scope == "head":
        return 0, (record[: spec.window] if spec.window else record)
    if spec.scope == "tail":
        if spec.window:
            offset = max(0, len(record) - spec.window)
            return offset, record[offset:]
        return 0, record
    return 0, record


def _group_value(m: "re.Match[str]", spec: FieldSpec) -> str:
    """Which part of a match is the field value: ?P<v>, else group 1, else all."""
    if "v" in (m.groupdict() or {}):
        val = m.group("v")
    elif m.lastindex:
        val = m.group(1)
    else:
        val = m.group(0)
    return val.strip() if spec.strip else val


def _match_line(text: str, spec: FieldSpec) -> _Hit:
    """Apply a `line` field's pattern to its line: no match means no value."""
    m = re.compile(spec.pattern or "").search(text)
    if not m:
        return _Hit(status="no-match")
    val = _group_value(m, spec)
    if spec.min_len and len(val) < spec.min_len:
        return _Hit(status="no-match", detail=f"太短（要求 ≥{spec.min_len} 字符）")
    if spec.exclude and any(x in val for x in spec.exclude):
        return _Hit(status="no-match", detail="命中排除词")
    return _Hit(raw=val)


def _one(record: list[str], spec: FieldSpec, done: dict[str, Any]) -> _Hit:
    if spec.kind == "const":
        return _Hit(raw=spec.value)

    if spec.kind == "line":
        idx = spec.index if spec.index >= 0 else len(record) + spec.index
        if not (0 <= idx < len(record)):
            # The card points past the end of the record: a rule/source mismatch,
            # not an absent value. Naming it saves the user from hunting the page.
            return _Hit(status="out-of-range",
                        detail=f"记录只有 {len(record)} 行，规则要第 {spec.index} 行")
        text = record[idx]
        if not spec.pattern:
            return _Hit(raw=text, line=idx)
        # A line field with a pattern means "this line, but keep only the part the
        # pattern marks". Without this the pattern was accepted by the schema and
        # then silently ignored, so `负责人：甲一` came back with its label while
        # the card plainly asked for `甲一` -- a wrong value that still counts as
        # 100% filled.
        hit = _match_line(text, spec)
        return _Hit(raw=hit.raw, status=hit.status, line=idx, detail=hit.detail)

    if spec.kind == "cell":
        # A cell field reads the record's single TSV line.
        line = record[0] if record else None
        if line is None:
            return _Hit(status="out-of-range", detail="记录是空的")
        cells = line.split("\t")
        i = spec.col or 0
        if not (0 <= i < len(cells)):
            return _Hit(status="out-of-range",
                        detail=f"这条记录只有 {len(cells)} 列，规则要第 {i + 1} 列")
        return _Hit(raw=cells[i], line=0)

    if spec.kind == "search":
        pat = re.compile(spec.pattern or "")
        offset, lines = _candidates(record, spec)
        for no, line in enumerate(lines):
            m = pat.search(line)
            if not m:
                continue
            val = _group_value(m, spec)
            if spec.min_len and len(val) < spec.min_len:
                continue
            if spec.exclude and any(x in val for x in spec.exclude):
                continue
            return _Hit(raw=val, line=offset + no)
        return _Hit(status="no-match")

    if spec.kind == "span":
        lo, hi = _span_bounds(record, spec, done)
        if lo is None or hi is None or hi <= lo:
            return _Hit(status="missing",
                        detail="两个边界之间的范围是空的（标记没找到或顺序反了）")
        chunk = "\n".join(record[lo:hi]).strip()
        if spec.exclude and any(x in chunk for x in spec.exclude):
            return _Hit(status="no-match", detail="命中排除词")
        if spec.min_len and len(chunk) < spec.min_len:
            return _Hit(status="no-match", detail=f"太短（要求 ≥{spec.min_len} 字符）")
        return _Hit(raw=chunk, line=lo)

    # dom / json have no producer in this build. The schema refuses them before
    # we get here; this branch is the honest answer if one slips through.
    return _Hit(status="unimplemented", detail=f"kind={spec.kind} 没有生产者")


def _span_bounds(record: list[str], spec: FieldSpec,
                 done: dict[str, Any]) -> tuple[int | None, int | None]:
    """Both bounds, or (None, None) when a declared bound cannot be resolved.

    A declared bound that is not found is a rule/source mismatch, not "the
    whole record": returning the record length there would hand back every
    line as this field's value, filled and wrong.
    """
    lo = 0
    if spec.start:
        lo = _find_bound(record, spec.start, done, default=0)
        if lo is None:
            return None, None
    hi = len(record)
    if spec.end:
        found = _find_bound(record, spec.end, done, default=len(record))
        if found is None:
            return None, None
        hi = found
    return lo, hi


def _find_bound(record: list[str], expr: str, done: dict[str, Any],
                *, default: int) -> int | None:
    """Resolve `after:X` / `before:X` / `line:N` to a line index.

    A bound that is declared but cannot be located is None: the caller turns
    that into an empty field with a reason, so a card whose marker has gone
    missing fails loudly instead of silently spanning the whole record.

    `before?:X` / `after?:X` mark the bound optional: when the marker is absent
    the bound falls back to `default` (record start for `after`, record end for
    `before`). That is how a card says "stop at 规格 if it is there" without
    losing the field on records that have no 规格 line. Strict by default,
    because a missing marker usually means the page changed under the card.
    """
    head, _, arg = expr.partition(":")
    head, arg = head.strip(), arg.strip()
    optional = head.endswith("?")
    if optional:
        head = head[:-1].strip()

    def not_found() -> int | None:
        return default if optional else None

    if head == "line":
        try:
            n = int(arg)
        except ValueError:
            return not_found()
        return max(0, min(len(record), n if n >= 0 else len(record) + n))

    if head == "after":
        # 1) an earlier field's value: start just past the line containing it
        val = done.get(arg)
        if isinstance(val, str) and val:
            first = val.split("\n", 1)[0].strip()
            for i, line in enumerate(record):
                if line.strip() == first:
                    return i + 1
            return not_found()
        # 2) otherwise treat the argument as a literal marker
        for i, line in enumerate(record):
            if arg and arg in line:
                return i + 1
        return not_found()

    if head == "before":
        # 1) an earlier field's value
        val = done.get(arg)
        if isinstance(val, str) and val:
            first = val.split("\n", 1)[0].strip()
            for i, line in enumerate(record):
                if line.strip() == first:
                    return i
        # 2) a regex
        try:
            pat = re.compile(arg)
        except re.error:
            pat = None
        if pat is not None:
            for i, line in enumerate(record):
                if pat.search(line):
                    return i
        # 3) a literal
        for i, line in enumerate(record):
            if arg and arg in line:
                return i
        return not_found()

    return not_found()


_DATE_RE = re.compile(
    r"(?P<y>\d{4})\s*[-/年.]\s*(?P<m>\d{1,2})\s*[-/月.]\s*(?P<d>\d{1,2})(?!\d)")


def _iso_date(s: str) -> str | None:
    """`2026年3月5日` / `2026/3/5` / `2026-03-05` -> `2026-03-05`.

    None when the text is not unambiguously one calendar date, because a guess
    here would look like data. The original string is never replaced by this.

    Validity is decided by `datetime.date`, not by `1 <= d <= 31`. The range
    check accepted 2026-02-31 and 2026-04-31 and emitted them as normalized
    dates; a date that does not exist is not a date, and a downstream consumer
    has no way to tell it apart from one that does.
    """
    m = _DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


# --- number parsing -------------------------------------------------------
#
# The old implementation ran `re.search(r"-?\d+", s)` and returned the first
# fragment it found. That turns "10–20" into 10, "1.5万" into 1.5 and "1e3" into
# 1 -- three numbers the page never stated -- and then reports them as `ok`,
# so the field-status machinery cannot point at the loss either.
#
# Extraction is the card's job (its pattern decides which characters are the
# value). Conversion is this function's job, and it is all-or-nothing: the
# whole remainder must be one number, or the field is `coerce-failed`.
_THOUSANDS = str.maketrans("", "", ",，\u00a0\u3000 ")
# Currency symbols are magnitude-neutral decoration: "¥1,280" and "1,280" are
# the same amount of money. They are stripped without asking, unlike units,
# which change what the number means ("1.5万" is not 1.5) and must be declared.
_CURRENCY = "¥￥$€£₩₹₽¢"
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_RANGE_RE = re.compile(
    r"^(?P<a>[+-]?\d+(?:\.\d+)?)\s*(?:[–—~～]|\.\.|至|到|-)\s*(?P<b>[+-]?\d+(?:\.\d+)?)$")
_PERCENT_RE = re.compile(r"^(?P<num>[+-]?\d+(?:\.\d+)?)\s*[%％]$")


def _number_text(raw: Any, spec: "FieldSpec") -> tuple[str, bool] | None:
    """Strip the card's declared decorations. None = the shape is not a number.

    Returns (bare number text, was_percent). Only decorations the card opted
    into are removed, so an undeclared "万" leaves a non-numeric string and the
    conversion fails loudly instead of silently dropping the unit.
    """
    s = str(raw).strip()
    if not s:
        return None
    s = s.strip(_CURRENCY).strip()
    was_percent = False
    m = _PERCENT_RE.match(s)
    if m:
        if not spec.percent:
            return None
        s = m.group("num")
        was_percent = True
    if spec.unit:
        unit = spec.unit
        if s.endswith(unit):
            s = s[: -len(unit)].rstrip()
        elif s.startswith(unit):
            s = s[len(unit):].lstrip()
        else:
            return None
    s = s.translate(_THOUSANDS)
    return s, was_percent


def _coerce_number(raw: Any, typ: str, spec: "FieldSpec") -> Any:
    parsed = _number_text(raw, spec)
    if parsed is None:
        return None
    s, _was_percent = parsed
    rng = _RANGE_RE.match(s)
    if rng:
        if spec.on_range == "reject":
            return None
        s = rng.group("a") if spec.on_range == "first" else rng.group("b")
    if typ == "int":
        if not _INT_RE.match(s):
            return None
        val: Any = int(s)
    else:
        if not _FLOAT_RE.match(s):
            return None
        val = float(s)
    if spec.multiplier is not None:
        val = val * spec.multiplier
        if typ == "int":
            val = int(round(val))
    return val


def _coerce(raw: Any, spec: "FieldSpec") -> Any:
    if raw is None:
        return None
    typ = spec.type
    if typ in ("text", "str"):
        s = str(raw).strip()
        return s or None
    if typ == "date":
        s = str(raw).strip()
        return s or None      # kept verbatim: interpreting dates is the caller's job
    if typ in ("int", "float"):
        return _coerce_number(raw, typ, spec)
    if typ == "bool":
        low = str(raw).strip().lower()
        if low in ("true", "1", "yes", "是", "有"):
            return True
        if low in ("false", "0", "no", "否", "无", "没"):
            return False
        return None
    s = str(raw).strip()
    return s or None

