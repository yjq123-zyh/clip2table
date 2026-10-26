"""Card schema: the validation rules, in one place.

A card is data, not code. Validating it is the difference between "add a site
by adding a file" and "add a site by reading a traceback".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import InputError
from ..capabilities import check_kind
from ..pipeline.extract import KINDS, TYPES, FieldSpec

CHANNELS = ("http", "browser", "desktop")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TOP_KEYS = {"schema", "name", "channel", "harvest", "target", "fetch",
            "segment", "fields", "paging", "dedupe", "samples", "task"}
SEGMENT_MODES = ("anchor", "blank", "single", "lines", "periodic")
# What a task says about itself. Kept inside the card file on purpose: a task
# that lives somewhere else drifts from the rules it claims to run, and the
# first thing a user does after a bad page is open the rules. `stats` is the
# one exception (see tasks.py) - it changes on every run and must not rewrite
# a hand-edited file.
TASK_KEYS = {"title", "purpose", "source", "sample", "created", "expected",
             "notes"}


@dataclass
class Card:
    name: str
    schema: int = 1
    channel: str | None = None
    fields: list[FieldSpec] = field(default_factory=list)
    anchor: str | None = None
    anchor_next: str | None = None
    min_lines: int = 1
    segment_mode: str = "anchor"
    segment_period: int | None = None
    segment_offset: int = 0
    segment_count: int | None = None
    skip_header: bool = False
    dedupe_key: list[str] = field(default_factory=list)
    on_collision: str = "report_only"
    # Which fields make two rows "the same record", and which are allowed to
    # change while it stays that record. Only a card can answer this: from the
    # data alone a changed price and a wrong key look identical. Both optional;
    # without them no collision is ever called an identity clash, only hinted.
    identity_fields: list[str] = field(default_factory=list)
    mutable_fields: list[str] = field(default_factory=list)
    max_pages: int = 1
    pause_ms: int = 1200
    paging_style: str = "none"
    paging_next: str | None = None
    # A URL template with {page}, for browser cards whose list is addressable.
    # This is what makes a browser harvest genuinely resumable: without it the
    # only way to reach page N is to click "next" N-1 times from wherever the
    # tab happens to be, which is a guess about the tab's current position.
    paging_url_template: str | None = None
    # How the loop decides the list is over. "fingerprint" (the default) stops
    # only on evidence the site gave us: max_pages, the next-page control
    # disappearing, or a page whose content repeats an earlier one. "empty"
    # adds the old heuristic (N pages with no new rows), kept as an opt-in
    # because a list with pinned or re-sorted items can legitimately hand back
    # an all-duplicate page while real records remain.
    stop_when: str = "fingerprint"
    stop_after_empty: int = 2
    # What a record is expected to look like. Both are optional and both only
    # produce a warning, never a silent drop: a record that is 2 lines when the
    # card says 4-6, or one whose 标题 came out empty, is still a record, and
    # deleting it would be the tool deciding the page is wrong.
    record_lines: tuple[int, int] | None = None
    required: list[str] = field(default_factory=list)
    harvest: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    fetch: dict[str, Any] = field(default_factory=dict)
    samples: str | None = None
    # `[task]`: who this card is for and what it is supposed to produce. All
    # optional, all display-only. Nothing in the pipeline reads it, so a card
    # without it runs exactly as before -- which is what lets this arrive
    # without a schema bump.
    task: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def title(self) -> str:
        """Display name: `[task].title`, else the card name."""
        t = self.task.get("title")
        return str(t) if isinstance(t, str) and t.strip() else self.name


def parse_card(data: dict[str, Any], path: str = "") -> Card:
    """Turn a parsed TOML dict into a Card, or raise InputError naming the key."""
    if not isinstance(data, dict):
        raise InputError(f"{path}: 卡的顶层必须是一张表")

    unknown = set(data) - TOP_KEYS
    if unknown:
        raise InputError(f"{path}: 不认识的顶层键 {sorted(unknown)}",
                         hint=f"允许的是 {sorted(TOP_KEYS)}")

    schema = data.get("schema")
    if schema != 1:
        raise InputError(f"{path}: schema 必须是 1，实际是 {schema!r}")

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise InputError(f"{path}: name 必须是 ^[a-z0-9][a-z0-9-]*$，实际是 {name!r}")

    channel = data.get("channel")
    if channel is not None and channel not in CHANNELS:
        raise InputError(f"{path}: channel 只能是 {CHANNELS}，实际是 {channel!r}")

    seg = data.get("segment")
    if not isinstance(seg, dict):
        raise InputError(f"{path}: 缺少 [segment]（记录边界是必填项）")
    mode = seg.get("mode", "anchor")
    if mode not in SEGMENT_MODES:
        raise InputError(f"{path}: [segment].mode 只能是 {SEGMENT_MODES}，实际是 {mode!r}")

    period, offset, count = seg.get("period"), seg.get("offset", 0), seg.get("count")
    if mode == "periodic":
        for key, value, minimum in (("period", period, 1), ("offset", offset, 0),
                                    ("count", count, 1)):
            if key == "count" and value is None:
                continue
            if type(value) is not int or value < minimum:
                raise InputError(f"{path}: [segment].{key} 必须是至少 {minimum} 的整数")
    elif any(k in seg for k in ("period", "offset", "count")):
        raise InputError(f"{path}: period / offset / count 只适用于 periodic 分段")

    anchor = seg.get("anchor")
    anchor_next = seg.get("anchor_next")
    if mode == "anchor" and not anchor:
        raise InputError(f"{path}: [segment].mode=\"anchor\" 但没给 anchor")
    for key, val in (("anchor", anchor), ("anchor_next", anchor_next)):
        if val is None:
            continue
        if not isinstance(val, str):
            raise InputError(f"{path}: [segment].{key} 必须是字符串正则")
        try:
            re.compile(val)
        except re.error as e:
            raise InputError(f"{path}: [segment].{key} 正则非法：{e}") from None

    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise InputError(f"{path}: 至少需要一个 [fields.<name>]")

    specs: list[FieldSpec] = []
    for fname, body in raw_fields.items():
        if not isinstance(body, dict):
            raise InputError(f"{path}: [fields.{fname}] 必须是一张表")
        kind = body.get("kind")
        if kind not in KINDS:
            raise InputError(f"{path}: [fields.{fname}].kind 只能是 {KINDS}，实际是 {kind!r}")
        # Declaring something the runtime cannot fill must fail here, not at
        # harvest time with an empty cell. `dom` and `json` are in the vocabulary
        # but have no producer, so a card asking for them would silently produce
        # None -- indistinguishable from "the page had no such value".
        check_kind(kind, channel, f"{path}: [fields.{fname}]")
        typ = body.get("type", "text")
        if typ not in TYPES:
            raise InputError(f"{path}: [fields.{fname}].type 只能是 {TYPES}，实际是 {typ!r}")

        kwargs: dict[str, Any] = {"name": fname, "kind": kind, "type": typ}
        for key in ("col", "index", "pattern", "scope", "window", "sel", "attr",
                    "path", "value", "min_len", "exclude", "strip",
                    "unit", "multiplier", "percent", "on_range"):
            if key in body:
                kwargs[key] = body[key]
        # span bounds: the card uses from/to, the dataclass uses start/end
        if "from" in body:
            kwargs["start"] = body["from"]
        if "to" in body:
            kwargs["end"] = body["to"]
        try:
            spec = FieldSpec(**kwargs)
        except ValueError as e:
            raise InputError(f"{path}: [fields.{fname}] 不合法：{e}") from None
        if spec.pattern:
            try:
                re.compile(spec.pattern)
            except re.error as e:
                raise InputError(f"{path}: [fields.{fname}].pattern 正则非法：{e}") from None
        specs.append(spec)

    dedupe = data.get("dedupe") or {}
    key = dedupe.get("key") or []
    if isinstance(key, str):
        key = [key]
    if not isinstance(key, list):
        raise InputError(f"{path}: [dedupe].key 必须是字符串或字符串数组")
    named = {s.name for s in specs}
    missing = [k for k in key if k not in named]
    if missing:
        raise InputError(f"{path}: [dedupe].key 引用了不存在的字段 {missing}")

    identity = dedupe.get("identity") or []
    if isinstance(identity, str):
        identity = [identity]
    if not isinstance(identity, list):
        raise InputError(f"{path}: [dedupe].identity 必须是字符串或字符串数组")
    bad = [k for k in identity if k not in named]
    if bad:
        raise InputError(f"{path}: [dedupe].identity 引用了不存在的字段 {bad}")

    mutable = dedupe.get("mutable") or []
    if isinstance(mutable, str):
        mutable = [mutable]
    if not isinstance(mutable, list):
        raise InputError(f"{path}: [dedupe].mutable 必须是字符串或字符串数组")
    bad = [k for k in mutable if k not in named]
    if bad:
        raise InputError(f"{path}: [dedupe].mutable 引用了不存在的字段 {bad}")
    overlap = sorted(set(identity) & set(mutable))
    if overlap:
        raise InputError(f"{path}: [dedupe] 里 {overlap} 同时被声明为身份字段和可变字段")

    collision = dedupe.get("on_collision", "report_only")
    if collision not in ("report_only", "keep_first", "keep_all"):
        raise InputError(f"{path}: [dedupe].on_collision 只能是 "
                         f"report_only/keep_first/keep_all，实际是 {collision!r}")

    paging = data.get("paging") or {}
    style = paging.get("style", "none")
    if style not in ("none", "click"):
        raise InputError(f"{path}: [paging].style 只能是 none/click，实际是 {style!r}")
    if style == "click" and not paging.get("next"):
        raise InputError(f"{path}: [paging].style=\"click\" 但没给 next")

    stop_when = paging.get("stop_when", "fingerprint")
    if stop_when not in ("fingerprint", "empty"):
        raise InputError(
            f"{path}: [paging].stop_when 只能是 fingerprint/empty，"
            f"实际是 {stop_when!r}",
            hint='fingerprint = 只按「末页/翻不动/内容重复」停；'
                 'empty = 再加上「连续 N 页没新增就停」这条经验规则')
    stop_after_empty = int(paging.get("stop_after_empty", 2))
    if stop_after_empty < 1:
        raise InputError(f"{path}: [paging].stop_after_empty 至少是 1，"
                         f"实际是 {stop_after_empty}")

    url_template = paging.get("url_template")
    if url_template is not None:
        if channel != "browser":
            raise InputError(
                f"{path}: [paging].url_template 只对 browser 卡有意义"
                f"（当前 channel={channel!r}）",
                hint="http 卡用 [fetch].url 里的 {page}；desktop 卡读不到地址栏")
        if "{page}" not in str(url_template):
            raise InputError(
                f"{path}: [paging].url_template 里必须有 {{page}} 占位符",
                hint='例如 url_template = "https://example.com/list?page={page}"')

    fetch = data.get("fetch") or {}
    if channel == "http":
        # Channel A has nothing else to go on: no window to aim at, no clipboard
        # to read. An http card without a url is a card that cannot work, so this
        # is caught here rather than at harvest time after the user waited.
        if not isinstance(fetch, dict) or not fetch.get("url"):
            raise InputError(
                f"{path}: channel=\"http\" 的卡必须有 [fetch] 段和 url",
                hint='加一段 [fetch] 和 url = "https://example.com/list?page={page}"')

    skip_header = bool(seg.get("skip_header", False))
    if skip_header and mode != "lines":
        raise InputError(
            f"{path}: [segment].skip_header 只在 mode=\"lines\" 时有意义，"
            f"现在是 mode={mode!r}",
            hint='把 mode 改成 "lines"，或者删掉 skip_header')

    samples = data.get("samples")
    if samples is not None and not isinstance(samples, str):
        raise InputError(
            f"{path}: samples 必须是字符串（相对卡文件的目录名），"
            f"实际是 {type(samples).__name__}",
            hint='写法是 samples = "samples/mysite"')

    record_lines = _record_lines(seg, path)
    required = seg.get("required") or []
    if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
        raise InputError(f"{path}: [segment].required 必须是字符串数组",
                         hint='写法是 required = ["标题", "日期"]')
    unknown_req = [r for r in required if r not in {s.name for s in specs}]
    if unknown_req:
        raise InputError(f"{path}: [segment].required 引用了不存在的字段 {unknown_req}",
                         hint=f"可用的字段是 {[s.name for s in specs]}")

    return Card(
        name=name, schema=1, channel=channel, fields=specs,
        anchor=anchor, anchor_next=anchor_next,
        min_lines=int(seg.get("min_lines", 1)), segment_mode=mode,
        segment_period=period, segment_offset=offset, segment_count=count,
        skip_header=skip_header,
        dedupe_key=list(key), on_collision=collision,
        identity_fields=list(identity), mutable_fields=list(mutable),
        max_pages=int(paging.get("max_pages", 1)),
        pause_ms=int(paging.get("pause_ms", 1200)),
        paging_style=style, paging_next=paging.get("next"),
        paging_url_template=(str(url_template) if url_template else None),
        stop_when=stop_when, stop_after_empty=stop_after_empty,
        harvest=dict(data.get("harvest") or {}),
        target=dict(data.get("target") or {}),
        fetch=dict(data.get("fetch") or {}),
        samples=samples, path=path,
        record_lines=record_lines, required=list(required),
        task=_task(data.get("task"), path),
    )


def _task(raw: Any, path: str) -> dict[str, Any]:
    """`[task]`: free-form, but the keys are checked so a typo is caught.

    Deliberately loose on values and strict on names. A task description is
    prose the user wrote; a misspelled key is a silence that shows up later as
    "why does my purpose not print anywhere".
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputError(f"{path}: [task] 必须是一张表",
                         hint='写法是 [task] 换行 title = "订单台账"')
    unknown = set(raw) - TASK_KEYS
    if unknown:
        raise InputError(f"{path}: [task] 里不认识的键 {sorted(unknown)}",
                         hint=f"允许的是 {sorted(TASK_KEYS)}")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in ("title", "purpose", "source", "sample", "created"):
            if not isinstance(v, str):
                raise InputError(f"{path}: [task].{k} 必须是字符串")
            out[k] = v
        elif k == "expected":
            # What the user says a correct record looks like. Kept as a list of
            # strings rather than a schema: it is read by a human in the preview
            # header, not by a validator.
            if isinstance(v, str):
                out[k] = [v]
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = list(v)
            else:
                raise InputError(f"{path}: [task].expected 必须是字符串或字符串数组")
        elif k == "notes":
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                raise InputError(f"{path}: [task].notes 必须是字符串数组")
            out[k] = list(v)
    return out


def _record_lines(seg: dict[str, Any], path: str) -> tuple[int, int] | None:
    """`[segment] record_lines = [min, max]`, validated, or None."""
    raw = seg.get("record_lines")
    if raw is None:
        return None
    if (not isinstance(raw, (list, tuple)) or len(raw) != 2
            or any(not isinstance(v, int) or isinstance(v, bool) for v in raw)):
        raise InputError(
            f"{path}: [segment].record_lines 必须是两个整数 [最少行, 最多行]，"
            f"实际是 {raw!r}",
            hint='写法是 record_lines = [4, 6]')
    lo, hi = int(raw[0]), int(raw[1])
    if lo < 1 or hi < lo:
        raise InputError(
            f"{path}: [segment].record_lines 要求 1 ≤ 最少行 ≤ 最多行，"
            f"实际是 [{lo}, {hi}]")
    return (lo, hi)
