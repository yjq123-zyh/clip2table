"""taskflow: the wizard behind `clip2table task new`, and the one-shot fixer.

Task creation, preview and correction:

  new    take a sample, guess the structure, let the user confirm or rename
         each field, then write a card they can run again without editing TOML.
  check  show the table beside the source, with a line number per value.
  fix    change one field's rule, re-apply it to the whole batch, and save, so
         one correction is not one hand-edit of a text file per page.

The interactive path is the point, but every prompt has a non-interactive
answer (`--yes` takes the guesses). A wizard that only works when a human is
typing is a wizard an agent cannot use, and this project is driven by agents.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cards import emit
from .errors import InputError
from .models import ParseResult
from .pipeline.extract import FieldSpec
from .preview import field_sources, grid, numbered_source


# ------------------------------------------------------------------ prompting

@dataclass
class Answers:
    """Everything `task new` needs, whether typed or defaulted."""

    name: str
    title: str
    purpose: str
    source: str
    channel: str
    fields: list[FieldSpec]
    segment_lines: str          # the TOML lines for [segment]
    dedupe: list[str]
    expected: list[str]
    on_collision: str = "report_only"
    identity: list[str] = field(default_factory=list)
    mutable: list[str] = field(default_factory=list)
    anchor: str = ""
    anchor_next: str = ""
    skip_header: bool = False
    # `[segment]` has two more constraints that a converted card may already
    # rely on. Dropping them turned a card that demanded "at least 3 lines per
    # record" into one that accepts a stray line as a record of its own.
    min_lines: int = 1
    required: list[str] = field(default_factory=list)
    record_lines: tuple[int, int] | None = None
    # The source the task points at, carried as-is so a saved task can reach its
    # own page instead of whatever happens to be on the clipboard. Each of these
    # is a card section verbatim; empty means "this channel cannot prove one".
    fetch: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    harvest: dict[str, Any] = field(default_factory=dict)
    paging: dict[str, Any] = field(default_factory=dict)
    samples: str | None = None
    # Why there is no `[paging]` when the user asked for more than one page.
    # Silence there reads as "it will page by itself", which it will not.
    paging_note: str = ""


def _ask(prompt: str, default: str = "", *, assume: bool) -> str:
    """One question. `assume` answers with the default and prints why."""
    if assume:
        print(f"{prompt}{default}  ← 用推断出来的（--yes）")
        return default
    if not sys.stdin.isatty():
        raise InputError(
            "任务向导需要交互确认，而当前不是交互终端",
            hint="加 --yes 直接用推断结果；或者先 --dry-run 看一眼再决定")
    got = input(f"{prompt}{default and f'[{default}]' or ''} ").strip()
    return got or default


def _ask_fields(res: ParseResult, specs: list[FieldSpec],
                assume: bool) -> list[FieldSpec]:
    """Confirm or rename each inferred column.

    Renaming is not cosmetic: the field name becomes the header of every future
    export, so getting it right here is the difference between a task that is
    reusable and one that produces a table someone renames by hand each time.
    """
    out: list[FieldSpec] = []
    for i, old in enumerate(res.fields, 1):
        sample = ""
        for r in res.rows:
            v = r.values.get(old)
            if v not in (None, ""):
                sample = str(v)
                break
        if len(sample) > 24:
            sample = sample[:23] + "…"
        shown = f"（例如：{sample}）" if sample else "（这一列全空）"
        new = _ask(f"  {i}. 字段「{old}」叫什么名字？{shown} ", old, assume=assume)
        new = new.strip() or old
        if new != old:
            out.append(_rename_spec(old, new, specs))
        else:
            out.append(_spec_for(old, specs))
    # Duplicate names would silently overwrite a column in every export.
    seen: dict[str, int] = {}
    for spec in out:
        seen[spec.name] = seen.get(spec.name, 0) + 1
    dupes = [n for n, c in seen.items() if c > 1]
    if dupes:
        raise InputError(f"字段名重复：{dupes}",
                         hint="每列必须有唯一的名字，否则导出时会互相覆盖")
    return out


def _spec_for(name: str, specs: list[FieldSpec]) -> FieldSpec:
    for s in specs:
        if s.name == name:
            return s
    return FieldSpec(name=name, kind="line", index=0)


def _rename_spec(old: str, new: str, specs: list[FieldSpec]) -> FieldSpec:
    """Same rule, new name. The rule is what was inferred; only the label moves."""
    base = _spec_for(old, specs)
    return FieldSpec(**{**{k: v for k, v in base.__dict__.items()
                           if k != "name"}, "name": new})


def build_answers(res: ParseResult, inf, cap, args, card=None,
                  seg_override: tuple[str, str, str] | None = None) -> Answers:
    """Collect the answers, in the order a person would ask them.

    `card` is the card being converted (`task new -p existing.toml`). When it is
    given its rules are the starting point, not the fallback: the whole point of
    converting a working card is to keep the rules that already work and only
    add the task metadata. Converting used to drop them -- `inf` is None on that
    path, so every field fell back to `line index=i` and the segmentation fell
    back to an anchor rule that was never inferred, producing a card that could
    not even load (`mode="anchor"` with no `anchor`).
    """
    assume = bool(getattr(args, "yes", False))
    level = inf.level if inf is not None else res.level
    if card is not None:
        specs = list(card.fields)
    elif inf is not None:
        specs = list(inf.specs or [])
    else:
        specs = []
    if not specs:
        # No rules to reuse: fall back to positional slices, which is what
        # `infer` itself does for S1-style columns.
        specs = [FieldSpec(name=f, kind="line", index=i)
                 for i, f in enumerate(res.fields)]
    specs = _align_specs_to(res, specs)
    default_name = f"task-{level.lower()}"
    if len(res.fields) == 2:
        default_name = f"task-{res.fields[0]}-{res.fields[1]}"[:40]
    name = _ask("任务名（英文、小写、连字符，用作文件名）？ ", default_name,
                assume=assume)
    name = _slug(name)

    print("\n先看一眼推断出来的表格：")
    print(grid(res, limit=int(getattr(args, "rows", 5) or 5)).render())
    print()

    title = _ask(f"这个任务叫什么（中文描述）？ ", f"{level} 任务", assume=assume)
    purpose = _ask("它要产出什么，给谁看？ ", "", assume=assume)
    # The sentence a person writes here is for them. The part that makes the
    # task runnable is derived, and shown so a "task" that can only read the
    # clipboard says so at the moment it is created rather than on page two.
    plan = source_plan(cap, card)
    source = _ask("数据来自哪里（下次你还要回到这个页面）？ ",
                  plan or _source_hint(cap), assume=assume)

    print("\n每个字段的名字会变成导出表头，现在改比以后改省事：")
    fields = _ask_fields(res, specs, assume)

    renamed = {old.name: new.name for old, new in zip(specs, fields)}
    defaults = ([renamed[k] for k in card.dedupe_key] if card is not None
                else [fields[0].name] if fields else [])
    if assume:
        dedupe = defaults
    else:
        key = _ask("\n去重字段（多个用逗号分隔，- 表示关闭）？ ",
                   ",".join(defaults) or "-", assume=False)
        dedupe = [] if key == "-" else [k.strip() for k in key.split(",")]
        if any(k not in {f.name for f in fields} for k in dedupe):
            raise InputError("去重字段必须使用表格中的字段名")

    expected = list(card.task.get("expected", [])) if card is not None else []
    if not assume:
        # `--yes` skips this one. "What does a correct record look like" is the
        # one question no sample can answer, so defaulting it to "y" only
        # produces an empty line and the appearance of having been asked.
        if _ask("\n要不要写一句「一条正确记录长什么样」（以后出问题好对照）？ ",
                "", assume=assume).lower().startswith("y"):
            line = _ask("  写成一句话： ", "", assume=assume)
            if line:
                expected = [line]

    seg_mode, seg_anchor, seg_next = _segment_parts(cap, level, inf, card)
    if seg_override is not None:
        # The user pointed at a boundary. Their reading of their own page
        # outranks anything inferred from it -- the inference is a guess that
        # happened to score well, not evidence about intent.
        seg_mode, seg_anchor, seg_next = seg_override
    chan = _pick_channel(cap, args, card)
    fetch = dict(card.fetch) if card is not None else {}
    target = dict(card.target) if card is not None else {}
    harvest_s = dict(card.harvest) if card is not None else {}
    paging = _paging_of(card) if card is not None else {}
    note = ""
    if card is None:
        fetch, target, paging, note = _infer_source(cap, chan)
    chan, fetch, target, paging = _configure_source(
        args, chan, fetch, target, paging, assume=assume)
    if chan == "browser" and target.get("url"):
        from urllib.parse import urlsplit
        url = urlsplit(target["url"])
        hint = f"{url.scheme}://{url.netloc}{url.path}"
        if card is not None and target.get("url") != card.target.get("url"):
            harvest_s["url_contains"] = hint
        else:
            harvest_s.setdefault("url_contains", hint)
    if fetch or target:
        note = ""

    # `[segment].required` names fields, so a renamed field would leave a
    # dangling reference that the card parser rejects outright. Follow the new
    # names; drop the ones whose field was not kept at all.
    required: list[str] = []
    if card is not None and card.required:
        renamed = {old.name: new.name for old, new in zip(specs, fields)}
        required = [renamed[r] for r in card.required if r in renamed]

    return Answers(
        name=name, title=title or name, purpose=purpose, source=source,
        channel=chan, fields=fields,
        segment_lines=seg_mode, dedupe=dedupe, expected=expected,
        on_collision=(card.on_collision if card is not None else "report_only"),
        identity=([renamed[k] for k in card.identity_fields] if card is not None else []),
        mutable=([renamed[k] for k in card.mutable_fields] if card is not None else []),
        anchor=seg_anchor, anchor_next=seg_next,
        skip_header=(card.skip_header if card is not None
                     else _has_header(cap, level, res)),
        min_lines=(card.min_lines if card is not None else 1),
        required=required,
        record_lines=(card.record_lines if card is not None else None),
        fetch=fetch, target=target, harvest=harvest_s, paging=paging,
        samples=(card.samples if card is not None else None),
        paging_note=note)


def _configure_source(args, channel, fetch, target, paging, *, assume):
    """Collect the existing acquisition engine's target and pagination settings."""
    if not assume:
        channel = _ask("采集通道（browser / http / desktop）？ ", channel,
                       assume=False)
    if channel not in ("browser", "http", "desktop"):
        raise InputError("采集通道必须是 browser、http 或 desktop")
    default = (fetch.get("url", "") if channel == "http" else
               target.get("url", "") if channel == "browser" else
               target.get("title", ""))
    source = getattr(args, "source", None)
    if source is None:
        source = default if assume else _ask(
            "来源地址（URL，可含 {page}）？ " if channel != "desktop"
            else "目标窗口标题（正则）？ ", default, assume=False)
    if not source:
        if not assume and not target.get("exe") and not target.get("window_class"):
            raise InputError("请填写可再次执行的来源")
        if getattr(args, "next", None) or getattr(args, "max_pages", None):
            raise InputError("设置翻页前需要提供 --source")
        return channel, fetch, target, paging
    if channel in ("http", "browser") and not source.startswith(("http://", "https://")):
        raise InputError("来源地址必须以 http:// 或 https:// 开头")
    template = source if "{page}" in source else _page_template(source)
    if channel == "browser" and source != default:
        paging.pop("url_template", None)
    next_step = getattr(args, "next", None)
    if channel != "http" and next_step is None:
        old = (paging.get("next", "") if channel == "browser"
               else target.get("next_key", ""))
        next_step = old if assume else _ask(
            "下一页按钮 CSS（使用页码地址时留空）？ " if channel == "browser"
            else "翻页按键（如 pagedown，单页留空）？ ", old, assume=False)
    if channel == "http":
        if next_step:
            raise InputError("HTTP 翻页使用 --source 中的 {page}；按钮翻页请选择 browser")
        fetch["url"] = template or source
    else:
        target["url" if channel == "browser" else "title"] = source.replace("{page}", "1")
        if channel == "browser":
            if next_step:
                paging.pop("url_template", None)
                paging.update(style="click", next=next_step)
            elif template:
                paging.pop("next", None)
                paging.update(style="none", url_template=template)
        elif next_step:
            target["next_key"] = next_step
    default_pages = paging.get("max_pages", 2 if template or next_step else 1)
    pages = getattr(args, "max_pages", None)
    if pages is None:
        pages = default_pages if assume else _ask("采集页数？ ", str(default_pages), assume=False)
    try:
        pages = int(pages)
    except (TypeError, ValueError):
        raise InputError("采集页数必须是正整数")
    if pages < 1:
        raise InputError("采集页数必须是正整数")
    if pages > 1 and not (template or next_step or paging.get("url_template")):
        raise InputError("多页采集需要页码地址、下一页按钮或翻页按键")
    paging["max_pages"] = pages
    return channel, fetch, target, paging


def _infer_source(cap, channel: str) -> tuple[dict, dict, dict, str]:
    """Card sections that point back at where this sample came from.

    Only what the capture can actually prove. A text file says nothing about
    the site it was copied from, so it gets nothing -- and the wizard prints
    that out rather than writing a task that quietly reads the clipboard.

    `{page}` is added only when the address already ends in a page number we
    can vary. Guessing a paging scheme from one page is how a task ends up
    fetching page 2 of a list that never existed.
    """
    url = (getattr(cap, "observed_url", "")
           or getattr(cap, "requested_url", "") or "")
    if not url or channel not in ("http", "browser"):
        return {}, {}, {}, ""
    tmpl = _page_template(url)
    if channel == "http":
        paging: dict[str, Any] = {"max_pages": 2} if tmpl else {}
        return {"url": tmpl or url}, {}, paging, (
            "" if tmpl else
            "这份样本的地址里没有页码，[fetch].url 只能取一页；"
            "要往后翻，把它改成带 {page} 的地址")
    target = {"url": url}
    paging = {}
    note = ""
    if tmpl:
        # Start with two pages so the saved template exercises pagination.
        paging["url_template"] = tmpl
        paging["max_pages"] = 2
    else:
        note = ("这份样本的地址里没有页码，只能采当前这一屏；"
                "要往后翻，加 [paging] url_template = \"...page={page}\" "
                "或 style = \"click\" next = \"选择器\"")
    return {}, target, paging, note


_PAGE_IN_URL = re.compile(r"(?P<pre>[?&](?:page|p|pn|pageNo|pageNum)=)(?P<n>\d+)(?P<post>.*)$",
                          re.IGNORECASE)


def _page_template(url: str) -> str:
    """`...?page=3` -> `...?page={page}`, else ""."""
    hit = _PAGE_IN_URL.search(url)
    if not hit:
        return ""
    return url[:hit.start("n")] + "{page}" + hit.group("post")


def _align_specs_to(res: ParseResult, specs: list[FieldSpec]) -> list[FieldSpec]:
    """Make sure every result field has a spec, in the result's column order.

    `infer` builds specs for the columns it emitted, but a caller may hand in a
    different result (a card run, or a re-parse). A missing spec would crash the
    renamer later; a wrong order would rename the wrong column.
    """
    by_name = {s.name: s for s in specs}
    out: list[FieldSpec] = []
    for i, f in enumerate(res.fields):
        out.append(by_name.get(f) or FieldSpec(name=f, kind="line", index=i))
    return out


def _slug(s: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        raise InputError("任务名不能为空，且只能用小写字母、数字、连字符")
    return s


def _source_hint(cap) -> str:
    url = getattr(cap, "observed_url", "") or getattr(cap, "requested_url", "") or ""
    return url


def _pick_channel(cap, args, card=None) -> str:
    pinned = getattr(args, "channel", "auto")
    if pinned and pinned != "auto":
        return pinned
    # A converted card already knows where it reads from. Re-deriving the
    # channel from the sample would turn a browser task into a desktop one just
    # because the sample arrived as a text file.
    if card is not None and card.channel in ("http", "browser", "desktop"):
        return card.channel
    ch = getattr(cap, "channel", "") or ""
    # A file capture says nothing about where the data lives next time. Storing
    # "file" as the channel would make a task that can never run again.
    if ch in ("http", "browser", "desktop"):
        return ch
    return "desktop"


def _paging_of(card) -> dict[str, Any]:
    """The card's `[paging]` as a plain dict, defaults omitted."""
    out: dict[str, Any] = {}
    if card.paging_style and card.paging_style != "none":
        out["style"] = card.paging_style
    if card.paging_next:
        out["next"] = card.paging_next
    if card.paging_url_template:
        out["url_template"] = card.paging_url_template
    if card.max_pages and card.max_pages != 1:
        out["max_pages"] = card.max_pages
    if card.stop_when and card.stop_when != "fingerprint":
        out["stop_when"] = card.stop_when
    if card.stop_after_empty and card.stop_after_empty != 2:
        out["stop_after_empty"] = card.stop_after_empty
    if card.pause_ms and card.pause_ms != 1200:
        out["pause_ms"] = card.pause_ms
    return out


def source_plan(cap, card=None) -> str:
    """What the task will reach for next time, and what it cannot promise.

    A task whose source is only a sentence ("订单后台") is a task that reads
    the clipboard and calls it automated. This works out, from the capture and
    the channel, which card section actually points at the data -- and says so
    out loud when the honest answer is "none of them".
    """
    if card is not None:
        if card.channel == "http" and (card.fetch or {}).get("url"):
            return f"直连 {(card.fetch or {}).get('url')}"
        if card.channel == "browser" and card.paging_url_template:
            return f"浏览器标签页 {card.paging_url_template}"
        if card.channel == "browser" and (card.target or {}).get("url"):
            return f"浏览器标签页 {(card.target or {}).get('url')}"
        if (card.target or {}).get("title") or (card.target or {}).get("exe"):
            return (f"窗口「{(card.target or {}).get('title') or (card.target or {}).get('exe')}」"
                    f"（采的时候会把里面的内容 Ctrl+A/Ctrl+C 出来）")
        return ""

    ch = getattr(cap, "channel", "") or ""
    url = getattr(cap, "observed_url", "") or getattr(cap, "requested_url", "") or ""
    if ch == "http" and url:
        return f"直连 {url}"
    if ch == "browser" and url:
        return f"浏览器标签页 {url}"
    if ch == "desktop":
        title = str((getattr(cap, "meta", {}) or {}).get("target_title") or "")
        if title:
            return f"窗口「{title}」（采的时候会把里面的内容 Ctrl+A/Ctrl+C 出来）"
    return ""


def _segment_parts(cap, level: str, inf, card=None) -> tuple[str, str, str]:
    """(mode line, anchor, anchor_next) - the three things [segment] needs.

    Recording the mode matters even when it is the default: a draft that says
    nothing about segmentation reads as "the tool decided", and the first
    question after a bad page is which decision it made.

    A card being converted keeps its own segmentation verbatim. Falling through
    to the inference path there produced `mode = "anchor"` with no `anchor` at
    all -- a card that its own parser rejects.
    """
    if card is not None:
        if card.segment_mode == "periodic":
            bits = ['mode = "periodic"', f"period = {card.segment_period}",
                    f"offset = {card.segment_offset}"]
            if card.segment_count is not None:
                bits.append(f"count = {card.segment_count}")
            return ("\n".join(bits), "", "")
        return (f'mode = "{card.segment_mode}"', card.anchor or "",
                card.anchor_next or "")
    if cap.formats and "CF_HTML" in cap.formats and level != "S0":
        return ('mode = "blank"', "", "")
    if level == "S0":
        return ('mode = "lines"', "", "")
    if level == "S4":
        return ('mode = "lines"', "", "")
    anchor = anchor_next = ""
    if inf is not None:
        from .cli import _anchor_rule, _second_line_rule
        anchor = _anchor_rule(inf)
        anchor_next = _second_line_rule(inf)
    return ('mode = "anchor"', anchor, anchor_next)


def _has_header(cap, level: str, res: ParseResult) -> bool:
    """Did `infer` read the first non-empty line as column names rather than data?

    This has to be asked, not assumed. `infer` drops the header when it spots
    one, so the finished table alone cannot say whether one was there -- but a
    card in `lines` mode starts a new record on every line, so leaving
    `skip_header` out puts the header row back in as a bogus record whose
    first cell is the word "订单号".
    """
    if level != "S0" or not res.rows:
        return False
    from .pipeline.normalize import normalize_text, to_lines
    from .core import _text_of
    lines = [ln for ln in to_lines(normalize_text(_text_of(cap))) if ln.strip()]
    if not lines:
        return False
    return res.rows[0].raw_lines[:1] != lines[:1]


def _toml_value(v: Any) -> str:
    """One TOML scalar, for the sections a wizard copies over verbatim."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return emit.toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return emit.toml_str(str(v))


def render_task_card(a: Answers) -> str:
    """The card file. Written to be read and edited by a human afterwards."""
    from .cli import _toml_key, _toml_key_str

    lines = [
        "schema = 1",
        f"name   = {_toml_key_str(a.name)}",
        f'channel = "{a.channel}"',
    ]
    if getattr(a, "samples", None):
        # A converted card keeps the regression samples it already had. Dropping
        # the line made `test <card>` fail on the converted copy while the
        # original still passed, which reads as "the conversion broke it".
        lines.append(f"samples = {emit.toml_str(a.samples)}")
    lines += [
        "",
        "[task]",
        f"title   = {_toml_key_str(a.title)}",
    ]
    if a.purpose:
        lines.append(f"purpose = {_toml_key_str(a.purpose)}")
    if a.source:
        lines.append(f"source  = {_toml_key_str(a.source)}")
    if a.expected:
        if len(a.expected) == 1:
            lines.append(f"expected = {_toml_key_str(a.expected[0])}")
        else:
            lines.append("expected = [")
            for e in a.expected:
                lines.append(f"  {_toml_key_str(e)},")
            lines.append("]")
    lines.append("")

    lines.append("[segment]")
    lines.extend(a.segment_lines.splitlines())
    if getattr(a, "min_lines", 1) > 1:
        lines.append(f"min_lines = {int(a.min_lines)}")
    if getattr(a, "record_lines", None):
        lo, hi = a.record_lines
        lines.append(f"record_lines = [{int(lo)}, {int(hi)}]")
    if getattr(a, "skip_header", False):
        lines.append("skip_header = true")
    if a.anchor:
        lines.append(f"anchor = {_toml_key_str(a.anchor)}")
    if a.anchor_next:
        lines.append(f"anchor_next = {_toml_key_str(a.anchor_next)}")
    elif a.segment_lines.startswith('mode = "anchor"'):
        # Say it out loud: an anchor with no `anchor_next` is a single-line
        # rule, which this project already proved cuts the wrong places.
        lines.append("# anchor_next 推断不出来：这版先用单条规则切，"
                     "务必用 task check 看过再采")
    if getattr(a, "required", None):
        lines.append("required = ["
                     + ", ".join(_toml_key_str(r) for r in a.required) + "]")
    lines.append("")

    # Where the task reads from. Without one of these the "task" is a parse
    # rule plus a note about where the data came from -- runnable only by hand,
    # one page at a time, which is the thing this stage exists to remove.
    if a.fetch:
        lines.append("[fetch]")
        for k, v in a.fetch.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    if a.target:
        lines.append("[target]")
        for k, v in a.target.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    if a.harvest:
        lines.append("[harvest]")
        for k, v in a.harvest.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    if a.paging:
        lines.append("[paging]")
        for k, v in a.paging.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    elif a.paging_note:
        lines.append(f"# {a.paging_note}")

    for f in a.fields:
        # One writer for field blocks, shared with `task fix --write`. Two
        # writers drifted apart and both dropped parameters the reader accepts,
        # so editing one rule silently changed others.
        lines.extend(emit.field_block(f))
        lines.append("")

    if a.dedupe or a.identity or a.mutable or a.on_collision != "report_only":
        lines.append("[dedupe]")
        lines.append("key = [" + ", ".join(emit.toml_str(d) for d in a.dedupe) + "]")
        lines.append(f"on_collision = {emit.toml_str(a.on_collision)}")
        if a.identity:
            lines.append("identity = " + _toml_value(a.identity))
        if a.mutable:
            lines.append("mutable = " + _toml_value(a.mutable))
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ fix

@dataclass
class Fix:
    """One correction to one field's rule."""

    field: str
    kind: str | None = None
    index: int | None = None
    col: int | None = None
    pattern: str | None = None
    type: str | None = None

    def apply(self, spec: FieldSpec) -> FieldSpec:
        kw = {k: v for k, v in spec.__dict__.items()}
        if self.kind is not None:
            kw["kind"] = self.kind
        if self.index is not None:
            kw["index"] = self.index
        if self.col is not None:
            kw["col"] = self.col
        if self.pattern is not None:
            kw["pattern"] = self.pattern
        if self.type is not None:
            kw["type"] = self.type
        # Switching primitive leaves stale coordinates behind, and a leftover
        # `index` on a `cell` rule is exactly the kind of thing that reads as
        # harmless and then silently picks the wrong line.
        if self.kind == "line":
            kw["col"] = None
            kw["pattern"] = self.pattern
        elif self.kind == "cell":
            kw["index"] = 0
            kw["pattern"] = None
        elif self.kind == "search":
            kw["index"] = 0
            kw["col"] = None
            if not (self.pattern or kw.get("pattern")):
                raise InputError("把规则改成 search 必须同时给出 --pattern")
        return FieldSpec(**kw)


def describe_fix(spec_before: FieldSpec, spec_after: FieldSpec) -> str:
    bits = []
    for k in ("kind", "index", "col", "pattern", "type"):
        a, b = getattr(spec_before, k), getattr(spec_after, k)
        if a != b:
            bits.append(f"{k}: {a!r} → {b!r}")
    return "；".join(bits) or "（规则没变）"


def show_effect(res_before: ParseResult, res_after: ParseResult,
                field: str, limit: int = 5) -> list[str]:
    """What the fix actually changed, per record. The whole justification."""
    out: list[str] = []
    changed = 0
    broke = 0
    for i, (a, b) in enumerate(zip(res_before.rows, res_after.rows), 1):
        va, vb = a.values.get(field), b.values.get(field)
        if va != vb:
            changed += 1
            if i <= limit:
                out.append(f"  第{i}条： {va or '∅'}  →  {vb or '∅'}")
        if va not in (None, "") and vb in (None, ""):
            broke += 1
    if not changed:
        out.append(f"  这条修正对「{field}」在整批样本上没有任何变化")
    else:
        out.append(f"  共 {changed} 条记录的这个字段变了")
    if broke:
        # Not fatal, but never silent: this is the shape of a fix that traded
        # one wrong column for another.
        out.append(f"  ⚠ 有 {broke} 条原本有值的记录现在空了")
    return out


def preview_text(res: ParseResult, lines: list[str], *, rows: int = 8,
                 source_lines: int = 0, field: str = "") -> list[str]:
    """The whole `task check` body, assembled in one place for reuse by tests."""
    out: list[str] = []
    out.append(f"共 {len(res.rows)} 条记录 × {len(res.fields)} 个字段"
               f"（未识别 {len(res.unparsed)} 行）")
    out.append("")
    out.extend(grid(res, limit=rows).lines)
    out.append("字段填充率：" + "；".join(
        f"{name} {rate:.0%}" for name, rate in res.fill_rates.items()))
    out.append("规则检查：" + ("；".join(res.deviations) if res.deviations else "通过"))
    if res.rows:
        out.append("")
        f = field or res.fields[0]
        out.append(f"「{f}」这一列的值分别来自原文的哪一行：")
        out.extend(field_sources(res, f, limit=rows))
    if source_lines:
        out.append("")
        out.append("原文（左边是行号，中间是它属于哪条记录，`·` 表示没人认领）：")
        out.extend(numbered_source(lines, res, limit=source_lines))
    return out
