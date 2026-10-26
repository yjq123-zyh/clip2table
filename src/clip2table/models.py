"""Core data types shared by every channel and pipeline stage.

One rule: a value that was not found in the source stays None. Nothing is
ever filled in, guessed, or borrowed from a neighbouring record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Captured:
    """What a channel hands to the pipeline. All three channels produce this.

    kind:
      "text"  - a plain text blob (clipboard TEXT/TSV, or a page's innerText)
      "html"  - HTML source (clipboard CF_HTML fragment, or a DOM snapshot)
      "json"  - a JSON document already parsed from an API response
    """

    kind: str
    payload: str | None = None
    json_payload: Any = None
    source_url: str = ""
    channel: str = ""          # http | browser | desktop
    engine: str = ""           # HTTP | E1 | E2 | None
    formats: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------- provenance
    # `source_url` above is kept for callers that just want "where did this
    # come from" as one string. The two fields below are the honest version,
    # and they are deliberately NOT the same field:
    #
    #   requested_url - what the caller asked for
    #   observed_url  - what the channel managed to prove it actually read
    #
    # The gap between them is the whole point. The desktop rung drives whatever
    # window is in front; it cannot read that window's address bar, so it cannot
    # prove the window shows the requested URL. Filling `source_url` with the
    # requested URL there produced a result that was neatly parsed, fully
    # conserved, and *from the wrong page* - a failure no downstream dedupe,
    # fill rate, or row count can catch. So an unprovable address stays empty
    # and the report says "未知", instead of echoing the request back.
    requested_url: str = ""
    observed_url: str = ""

    @property
    def url_is_proven(self) -> bool:
        """True only when the channel could show what it actually read."""
        return bool(self.observed_url)

    def provenance(self) -> dict[str, Any]:
        """The provenance block written next to every captured page."""
        return {
            "requested_url": self.requested_url or None,
            "observed_url": self.observed_url or None,
            "url_is_proven": self.url_is_proven,
            "channel": self.channel or None,
            "engine": self.engine or None,
            "target": {k: v for k, v in (self.meta or {}).items()
                       if k in ("target_title", "target_hwnd", "target_pid",
                                "target_url", "focus_method", "tab_id")},
        }

    def __post_init__(self) -> None:
        if self.kind not in ("text", "html", "json"):
            raise ValueError(f"unknown capture kind: {self.kind}")


@dataclass
class FieldTrace:
    """Why this field has this value -- or why it has none.

    The point is that "empty" is four different facts, and they were all being
    reported as the same empty cell:

      ok            - a value was found and converted
      missing       - the rule ran over the record and the record has no such thing
      out-of-range  - the rule points at line N / column N, the record is shorter
      no-match      - there was text, the pattern did not match it
      coerce-failed - text was found but is not an int/float/bool
      unimplemented - the build cannot execute this primitive at all
      rule-error    - the rule itself raised (bad regex at runtime, ...)

    Without this, a broken rule and a genuinely absent value look identical, and
    the user "fixes" the source instead of the rule.
    """

    field: str
    kind: str
    status: str = "ok"
    raw: str | None = None
    line: int | None = None      # which line of the record produced it
    detail: str = ""
    rule: str = ""               # the rule that produced it, verbatim
    normalized: str | None = None  # e.g. ISO date, when the value is a date

    @property
    def empty(self) -> bool:
        return self.status != "ok"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "status": self.status, "raw": self.raw,
                "line": self.line, "detail": self.detail, "rule": self.rule,
                "normalized": self.normalized}

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "FieldTrace":
        return cls(name, str(d.get("kind") or ""), str(d.get("status") or "ok"),
                   d.get("raw"), d.get("line"), str(d.get("detail") or ""),
                   str(d.get("rule") or ""), d.get("normalized"))

    def describe(self) -> str:
        where = f"第 {self.line} 行" if self.line is not None else "记录里"
        why = {
            "missing": f"{where}没有这条规则要的东西",
            "out-of-range": f"{where}不存在（规则指到记录外面去了）",
            "no-match": f"{where}有文本，但正则没匹配上",
            "coerce-failed": f"{where}取到了 {self.raw!r}，但不是 {self.detail or '目标类型'}",
            "unimplemented": "这个原语在当前版本里没有实现",
            "rule-error": f"规则本身报错：{self.detail}",
        }.get(self.status, self.detail or self.status)
        return f"{self.field}（{self.kind}）：{why}"


@dataclass
class Row:
    """One record. Keys are field names from the card (or inferred names)."""

    values: dict[str, Any]
    raw_lines: list[str] = field(default_factory=list)
    source_page: int | None = None
    # Index of this record's first line in the source text, so a preview can
    # point at 原文 instead of asking the reader to count records. None means
    # "the segmenter did not record it", which is different from 0.
    source_line: int | None = None
    # One source index per entry in `raw_lines`, same order. `source_line` alone
    # is only enough when a record's lines are contiguous, and they are not: a
    # record with a blank line in the middle drops that line, so every later
    # offset points one or more lines too early. Measured symptom: a value that
    # sits on source line 3 was reported as line 2, and line 3 was shown as
    # unclaimed. Empty means "not recorded", never "starts at 0".
    raw_at: list[int] = field(default_factory=list)
    # Not part of equality: two rows are the same record whether or not the
    # caller kept the audit trail.
    traces: dict[str, FieldTrace] = field(default_factory=dict, compare=False)

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def why_empty(self) -> list[str]:
        return [t.describe() for t in self.traces.values() if t.empty]


@dataclass
class ParseResult:
    """Output of segment+extract+assemble. Everything a report needs."""

    rows: list[Row]
    fields: list[str]
    unparsed: list[str] = field(default_factory=list)
    total_lines: int = 0
    # Lines that were consumed by a record, used for the conservation invariant.
    consumed_lines: int = 0
    level: str = ""            # S0..S4, or "card"
    notes: list[str] = field(default_factory=list)
    # Structural expectations the card declared, and this page missed. Empty
    # means "the page matched what the card says records look like". A page that
    # deviates is still parsed and still kept -- the raw text is always on disk --
    # but the report says which expectation failed instead of letting a changed
    # layout pass as clean data.
    deviations: list[str] = field(default_factory=list)
    deviation_rows: list[int] = field(default_factory=list)

    @property
    def fill_rates(self) -> dict[str, float]:
        """Per-field non-empty ratio. 0.0 when there are no rows."""
        if not self.rows:
            return {f: 0.0 for f in self.fields}
        out: dict[str, float] = {}
        for f in self.fields:
            filled = sum(1 for r in self.rows if _nonempty(r.values.get(f)))
            out[f] = filled / len(self.rows)
        return out

    def check_conservation(self) -> str | None:
        """The L2 invariant: every source line is either consumed or in unparsed.

        Returns None when the books balance, else a human-readable discrepancy.
        This catches LOST data. It cannot catch a record that was split in two -
        that one is caught by comparing against per-record expectations.
        """
        accounted = self.consumed_lines + len(self.unparsed)
        if self.total_lines == 0:
            return None
        if accounted == self.total_lines:
            return None
        delta = self.total_lines - accounted
        kind = "丢行" if delta > 0 else "凭空多出行"
        return (f"行数不守恒：源 {self.total_lines} 行，"
                f"已归入记录 {self.consumed_lines} 行 + 未识别 {len(self.unparsed)} 行 = "
                f"{accounted} 行，{kind} {abs(delta)} 行")


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    return True


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"
